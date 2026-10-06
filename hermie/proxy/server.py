"""The proxy server: one route per upstream, redact on the way out, restore on the way back, receipt per request.

`send_upstream` is the only function in the package that sends a request to an upstream, and it only accepts a
`CleanBody` (built by `gate.certify_body` after redaction).
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import secrets
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import AsyncIterator

import anyio
import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from hermie.config import Config
from hermie.gate.gate import Gate, certify_body, empty_body
from hermie.gate.redact import MappingStoreError
from hermie.gate.types import CleanBody
from hermie.proxy.approvals import AllowStore, Approvals, NoPrompter, PendingItem, SessionAllow
from hermie.proxy.receipt import BodyStore, Receipt, ReceiptLine, client_label
from hermie.proxy.stream import StreamStats, relay_sse, restore_json
from hermie.proxy.walker import Decision, walk_request

ROUTES: dict[str, str | None] = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com",
    "gemini": "https://generativelanguage.googleapis.com",
    "custom": None,
}

# Model listings carry no conversation: GET / DELETE with an empty body on these paths go out unchanged.
PASSTHROUGH_PREFIXES = ("/openai/v1/models", "/anthropic/v1/models", "/gemini/v1beta/models")
PASSTHROUGH_METHODS = ("GET", "DELETE")
REFUSED_PREFIXES = ("/openai/v1/files",)

FORWARD_HEADERS = {
    "authorization", "x-api-key", "x-goog-api-key", "anthropic-version", "anthropic-beta", "content-type", "accept",
    "user-agent", "openai-organization", "openai-project", "openai-beta", "x-goog-api-client", "x-stainless-*",
}
_FORWARD_EXACT = {h for h in FORWARD_HEADERS if not h.endswith("*")}
_FORWARD_PREFIX = tuple(h[:-1] for h in FORWARD_HEADERS if h.endswith("*"))

_BACK_EXACT = {"request-id", "x-request-id", "retry-after"}
_BACK_PREFIX = ("anthropic-ratelimit-", "x-ratelimit-")

UNSUPPORTED = "hermie only proxies JSON requests; this path is not in its passthrough list"
NO_FILES = "hermie does not proxy file uploads"
BAD_PATH = "hermie refuses paths with '.' or '..' segments or encoded slashes"
SEEN_IMAGES = 4096
BLOCKED = 'hermie blocked this message (id {id}): {reason}. Run "hermie allow {id}" to send it, then retry.'
UNSCANNED = ("hermie blocked this message (id {id}): {reason}. The text could not be scanned, so it cannot be sent "
             "or released; shorten it or retry.")


async def send_upstream(client: httpx.AsyncClient, method: str, url: str, headers: dict[str, str],
                        body: CleanBody, stream: bool) -> httpx.Response:
    """The only sender. Refuses anything the gate has not certified."""
    if not isinstance(body, CleanBody):
        raise TypeError("send_upstream needs a CleanBody from gate.certify_body")
    req = client.build_request(method, url, headers=headers, content=body.data)
    return await client.send(req, stream=stream)


def forward_headers(headers) -> dict[str, str]:
    out = {}
    for k, v in headers.items():
        low = k.lower()
        if low in _FORWARD_EXACT or low.startswith(_FORWARD_PREFIX):
            out[low] = v
    return out


def _back_headers(resp: httpx.Response, rid: str) -> dict[str, str]:
    out = {}
    for k, v in resp.headers.items():
        low = k.lower()
        if low in _BACK_EXACT or low.startswith(_BACK_PREFIX):
            out[low] = v
    out["x-hermie-request-id"] = rid
    return out


def _under(path: str, prefixes: tuple[str, ...]) -> str | None:
    return next((p for p in prefixes if path == p or path.startswith(p + "/")), None)


def is_passthrough(path: str) -> bool:
    """On the allowlist: a prefix itself or a path below it, but never a Gemini method call such as
    /gemini/v1beta/models/NAME:generateContent, which carries the conversation."""
    prefix = _under(path, PASSTHROUGH_PREFIXES)
    return prefix is not None and ":" not in path[len(prefix):]


def path_is_safe(decoded: str, raw: str) -> bool:
    """No dot segments (the upstream URL would collapse them onto another endpoint) and no encoded slash."""
    if "%2f" in raw.lower() or "%5c" in raw.lower() or "\\" in decoded:
        return False
    return not any(seg in (".", "..") for seg in decoded.split("/"))


def _is_json(content_type: str) -> bool:
    ct = content_type.split(";", 1)[0].strip().lower()
    return ct == "application/json" or ct.endswith("+json")


def _error(status: int, kind: str, message: str, rid: str, id_: str | None = None) -> JSONResponse:
    return JSONResponse({"error": {"type": kind, "message": message, "id": id_}}, status_code=status,
                        headers={"x-hermie-request-id": rid})


class _RelayResponse(StreamingResponse):
    """A StreamingResponse that owns the end of a relayed stream: whatever happens (normal end, client gone mid-stream,
    client gone before the first byte), it closes the relay generator, then calls `finish` exactly once.

    Starlette cannot be relied on for this: a failing `send` leaves a half-read generator for the garbage collector,
    `aclose()` on a generator that never started skips its `finally`, and `background` does not run after a disconnect."""

    def __init__(self, content, finish, **kw):
        super().__init__(content, **kw)
        self._finish = finish

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    aclose = getattr(self.body_iterator, "aclose", None)
                    if aclose is not None:
                        await aclose()
                finally:
                    await self._finish()


# --- judge accounting: a per-request counter carried into the walker thread by the context ---

@dataclass
class _JudgeTally:
    calls: int = 0
    ms: float = 0.0


_tally: contextvars.ContextVar[_JudgeTally | None] = contextvars.ContextVar("hermie_judge_tally", default=None)


class _CountingJudge:
    def __init__(self, inner):
        self.inner = inner

    def is_sensitive(self, text: str) -> bool:
        t0 = time.monotonic()
        try:
            return self.inner.is_sensitive(text)
        finally:
            tally = _tally.get()
            if tally is not None:
                tally.calls += 1
                tally.ms += (time.monotonic() - t0) * 1000

    def __getattr__(self, name):
        return getattr(self.inner, name)


# --- per-request bookkeeping for the receipt ---

@dataclass
class _Rec:
    rid: str
    client: str
    upstream: str
    mode: str
    model: str | None = None
    stream: bool = False
    scanned_bytes: int = 0
    replaced: dict[str, int] = field(default_factory=dict)
    withheld: list[str] = field(default_factory=list)
    held: str | None = None
    approved_by: str | None = None
    unrestored: int = 0
    status: int | None = None
    upstream_error: str | None = None
    new_parts: list[dict] = field(default_factory=list)
    tally: _JudgeTally = field(default_factory=_JudgeTally)

    def line(self) -> ReceiptLine:
        return ReceiptLine(
            at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), id=self.rid, client=self.client,
            upstream=self.upstream, model=self.model, stream=self.stream, scanned_bytes=self.scanned_bytes,
            replaced=dict(self.replaced), withheld=list(self.withheld), held=self.held, approved_by=self.approved_by,
            unrestored=self.unrestored, judge_calls=self.tally.calls, judge_ms=int(self.tally.ms),
            status=self.status, upstream_error=self.upstream_error, mode=self.mode, new_parts=list(self.new_parts))


def _part(d: Decision) -> dict:
    """A data-free receipt entry for one walker decision: origin, tool, size, entities and nothing else."""
    return {"origin": d.origin, "tool": d.tool, "size": d.size, "entities": list(d.entities)}


def _item_kind(d: Decision) -> str:
    if d.kind == "hold":
        return "message"
    return "image" if d.reason == "image" else "tool result"


def create_app(config: Config, gate: Gate | None = None, upstream_client: httpx.AsyncClient | None = None,
               prompter=None) -> Starlette:
    gate = gate if gate is not None else Gate(config)
    if gate.judge is not None and not isinstance(gate.judge, _CountingJudge):
        gate.judge = _CountingJudge(gate.judge)
    owns_client = upstream_client is None
    client = upstream_client or httpx.AsyncClient(timeout=httpx.Timeout(600, connect=30))
    prompter = prompter if prompter is not None else NoPrompter()
    store = AllowStore(config.data_dir)
    approvals = Approvals(store, SessionAllow())
    receipt = Receipt(config)
    bodies = BodyStore(config)
    seen_images: OrderedDict[str, None] = OrderedDict()

    def write_receipt(rec: _Rec) -> None:
        try:
            receipt.write(rec.line())
        except OSError:
            pass   # a full disk must not turn a delivered reply into an error

    def register(d: Decision) -> str:
        """Persist a held / withheld item under the id the walker already showed (both come from `store.assign`)."""
        return store.register(PendingItem(d.hash, _item_kind(d), d.reason, d.size, d.excerpt, d.id),
                              again=d.kind == "hold")

    async def walk(body: dict, rec: _Rec):
        """Walk, asking the user about held messages; return the result or a 422 response."""
        while True:
            ctx = contextvars.copy_context()
            ctx.run(_tally.set, rec.tally)
            result = await asyncio.to_thread(ctx.run, walk_request, body, gate, approvals, config)
            if result.held is None or config.mode == "observe":
                return result
            d = result.held
            if d.reason.startswith("detector_error"):   # never offered for release: only reject
                rec.held = rec.held or d.id
                return _error(422, "hermie_blocked", UNSCANNED.format(id=d.id, reason=d.reason), rec.rid, d.id)
            item_id = register(d)
            rec.held = rec.held or item_id
            item = store.pending.get(item_id) or PendingItem(d.hash, "message", d.reason, d.size, d.excerpt, item_id)
            item.excerpt = d.excerpt
            choice = await prompter.ask(item) if prompter.available else "reject"
            if choice == "send":
                store.allow(d.hash)
                rec.approved_by = "user"
            elif choice == "allow_all":
                approvals.session.all = True
                rec.approved_by = "session"
            else:
                return _error(422, "hermie_blocked", BLOCKED.format(id=item_id, reason=d.reason), rec.rid, item_id)

    async def passthrough(request: Request, url: str, rec: _Rec) -> Response:
        # Only body-less GET / DELETE get here: there is nothing to scan, so nothing is certified.
        resp = await send_upstream(client, request.method, url, forward_headers(request.headers),
                                   empty_body(), stream=False)
        rec.status = resp.status_code
        headers = _back_headers(resp, rec.rid)
        ct = resp.headers.get("content-type")
        if ct:
            headers["content-type"] = ct
        return Response(resp.content, status_code=resp.status_code, headers=headers)

    async def relay(resp: httpx.Response, stats: StreamStats, rec: _Rec) -> AsyncIterator[bytes]:
        try:
            async for chunk in relay_sse(resp.aiter_bytes(), gate.restore, stats):
                yield chunk
        except httpx.HTTPError as e:
            rec.upstream_error = type(e).__name__

    def stream_finisher(resp: httpx.Response, stats: StreamStats, rec: _Rec):
        """Receipt then upstream close, once; called from _RelayResponse in every outcome (unrestored is 0
        when the relay never started)."""
        async def finish() -> None:
            rec.unrestored = stats.unrestored
            try:
                write_receipt(rec)
            finally:
                await resp.aclose()   # idempotent
        return finish

    async def handle(request: Request) -> Response:
        rid = secrets.token_hex(6)
        provider = request.path_params["provider"]
        rec = _Rec(rid, client_label(request.headers.get("user-agent")), provider, config.mode)
        deferred = False   # True once a streaming response owns the receipt
        try:
            if provider not in ROUTES:
                rec.status = 404
                return _error(404, "hermie_unknown_route", f"unknown route /{provider}", rid)
            base = ROUTES[provider] if provider != "custom" else config.custom_upstream
            if not base:
                rec.status = 404
                return _error(404, "hermie_unknown_route", "no custom upstream configured", rid)
            path = request.url.path
            raw_path = request.scope.get("raw_path", path.encode()).decode("latin-1")
            if not path_is_safe(path, raw_path):
                rec.status = 400
                return _error(400, "hermie_bad_path", BAD_PATH, rid)
            # the upstream URL keeps the client's encoding; matching above used the decoded form
            rest = raw_path[len(provider) + 1:] if raw_path.startswith(f"/{provider}/") else path[len(provider) + 1:]
            url = base.rstrip("/") + rest + (f"?{request.url.query}" if request.url.query else "")
            raw = await request.body()

            if _under(path, REFUSED_PREFIXES):
                rec.status = 415
                return _error(415, "hermie_unsupported", NO_FILES, rid)
            if is_passthrough(path):
                if request.method not in PASSTHROUGH_METHODS or raw:
                    rec.status = 415
                    return _error(415, "hermie_unsupported", UNSUPPORTED, rid)
                rec.mode = "passthrough"
                try:
                    return await passthrough(request, url, rec)
                except httpx.HTTPError as e:
                    rec.upstream_error = type(e).__name__
                    rec.status = 502
                    return _error(502, "hermie_upstream_unreachable", type(e).__name__, rid)
            if not _is_json(request.headers.get("content-type", "")):
                rec.status = 415
                return _error(415, "hermie_unsupported", UNSUPPORTED, rid)
            try:
                body = json.loads(raw)
            except ValueError:
                body = None
            if not isinstance(body, dict):
                rec.status = 400
                return _error(400, "hermie_bad_request", "request body is not a JSON object", rid)
            model = body.get("model")
            rec.model = model if isinstance(model, str) else None
            stream = body.get("stream") is True or "alt=sse" in request.url.query.split("&")
            rec.stream = stream

            try:
                result = await walk(body, rec)
            except MappingStoreError as e:
                rec.status = 507
                return _error(507, "hermie_mapping_unwritable",
                              f"hermie could not write its placeholder mapping ({type(e).__name__})", rid)
            if isinstance(result, Response):
                rec.status = result.status_code
                return result
            rec.scanned_bytes = result.scanned_bytes
            rec.replaced = dict(result.counts)
            for d in result.decisions:
                if d.kind == "withhold":
                    rec.withheld.append(register(d))
                elif d.kind == "pass" and rec.approved_by is None:
                    rec.approved_by = "session" if approvals.session.all else "allowed"
                if d.origin == "binary":   # images never pass through the gate cache: remember them here
                    d.new = d.hash not in seen_images
                    seen_images[d.hash] = None
                    seen_images.move_to_end(d.hash)
                    while len(seen_images) > SEEN_IMAGES:
                        seen_images.popitem(last=False)
                if d.new:
                    rec.new_parts.append(_part(d))

            clean = certify_body(json.dumps(result.body, ensure_ascii=False).encode())
            bodies.put(rid, clean.data)
            try:
                resp = await send_upstream(client, request.method, url, forward_headers(request.headers), clean,
                                           stream=stream)
            except httpx.HTTPError as e:
                rec.upstream_error = type(e).__name__
                rec.status = 502
                return _error(502, "hermie_upstream_unreachable", type(e).__name__, rid)
            rec.status = resp.status_code
            headers = _back_headers(resp, rid)
            ct = resp.headers.get("content-type", "")

            if stream and ct.split(";", 1)[0].strip().lower() == "text/event-stream":
                stats = StreamStats()
                deferred = True
                return _RelayResponse(relay(resp, stats, rec), stream_finisher(resp, stats, rec),
                                      status_code=resp.status_code, headers=headers, media_type=ct)
            try:
                data = await resp.aread()
            except httpx.HTTPError as e:
                rec.upstream_error = type(e).__name__
                rec.status = 502
                return _error(502, "hermie_upstream_unreachable", type(e).__name__, rid)
            finally:
                await resp.aclose()
            if _is_json(ct):
                try:
                    obj, _ = restore_json(json.loads(data), gate.restore)
                    data = json.dumps(obj, ensure_ascii=False).encode()
                except ValueError:
                    pass
            if ct:
                headers["content-type"] = ct
            return Response(data, status_code=resp.status_code, headers=headers)
        finally:
            if not deferred:
                write_receipt(rec)

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            if owns_client:
                await client.aclose()

    methods = ["GET", "POST", "PUT", "PATCH", "DELETE"]
    app = Starlette(routes=[Route("/{provider}/{rest:path}", handle, methods=methods)], lifespan=lifespan)
    app.state.gate = gate
    app.state.approvals = approvals
    app.state.receipt = receipt
    app.state.bodies = bodies
    app.state.prompter = prompter
    app.state.config = config
    return app
