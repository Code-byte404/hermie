# tests/test_server.py
import json, httpx, pytest
from pathlib import Path
from starlette.testclient import TestClient
from hermie.config import Config
from hermie.gate.gate import Gate
from hermie.gate.redact import MappingStore
from hermie.gate.types import CleanBody, Origin
from hermie.proxy import server
from hermie.proxy.approvals import NoPrompter

FIX = Path(__file__).parent / "fixtures"
REAL = ("555-010-0199", "me@example.com", "AKIAIOSFODNN7EXAMPLE")

class Upstream:
    """Records what reached the upstream and replays a fixture stream or a JSON reply."""
    def __init__(self, stream_file=None, status=200, json_reply=None, raise_=None):
        self.seen = []; self.stream_file = stream_file; self.status = status; self.json_reply = json_reply; self.raise_ = raise_
    def __call__(self, req: httpx.Request):
        self.seen.append(req)
        if self.raise_: raise self.raise_
        if self.stream_file:
            return httpx.Response(self.status, headers={"content-type": "text/event-stream"}, content=(FIX / "streams" / self.stream_file).read_bytes())
        return httpx.Response(self.status, json=self.json_reply or {"content": [{"type": "text", "text": "dial <PHONE_NUMBER_1>"}]})

def _app(tmp_path, analyzer, up, judge=None, prompter=None, **kw):
    cfg = Config(data_dir=tmp_path, judge="ollama:x" if judge else None, **kw)
    gate = Gate(cfg, analyzer=analyzer, judge=judge, store=MappingStore(cfg.mapping_path))
    client = httpx.AsyncClient(transport=httpx.MockTransport(up))
    return cfg, TestClient(server.create_app(cfg, gate=gate, upstream_client=client, prompter=prompter or NoPrompter()))

def _body(fmt): return json.loads((FIX / "requests" / f"{fmt}.json").read_text())

@pytest.mark.parametrize("fmt,path", [("anthropic", "/anthropic/v1/messages"), ("openai_chat", "/openai/v1/chat/completions"),
                                      ("openai_responses", "/openai/v1/responses"), ("gemini", "/gemini/v1beta/models/gemini-2.5-pro:streamGenerateContent?alt=sse")])
def test_streaming_roundtrip_no_real_value_out_all_placeholders_back(fmt, path, tmp_path, analyzer):
    up = Upstream(stream_file=f"{fmt}.sse")
    cfg, c = _app(tmp_path, analyzer, up)
    body = _body(fmt) | {"stream": True}
    # seed the mapping the fixture streams expect
    MappingStore(cfg.mapping_path).add({"<PHONE_NUMBER_1>": "555-010-0199", "<SECRET_1>": "sk-test-abc123"})
    r = c.post(path, json=body, headers={"x-api-key": "sk-test-key", "user-agent": "claude-cli/2.1.291"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    sent = up.seen[0].content.decode()
    assert not any(v in sent for v in REAL) and up.seen[0].headers["x-api-key"] == "sk-test-key"
    assert "555-010-0199 works" in r.text and "<PHONE_NUMBER_1>" not in r.text and "sk-test-abc123" in r.text
    line = json.loads((tmp_path / "receipt.jsonl").read_text().splitlines()[-1])
    assert line["client"] == "claude-code" and line["unrestored"] == 0 and not any(v in json.dumps(line) for v in REAL)
    stored = json.loads(next((tmp_path / "outbound").glob("*.json")).read_text())
    assert json.dumps(stored) == sent          # verbatim receipt

def test_non_streaming_restores_json(tmp_path, analyzer):
    cfg, c = _app(tmp_path, analyzer, Upstream())
    r = c.post("/anthropic/v1/messages", json=_body("anthropic"))
    assert r.json()["content"][0]["text"] == "dial 555-010-0199"

def test_held_user_message_rejected_without_tty_then_sent_after_allow(tmp_path, analyzer):
    class J:
        def is_sensitive(self, t): return "<PHONE_NUMBER_" in t or "555" in t
    cfg, c = _app(tmp_path, analyzer, Upstream(), judge=J())
    r = c.post("/anthropic/v1/messages", json=_body("anthropic"))
    assert r.status_code == 422 and r.json()["error"]["type"] == "hermie_blocked"
    c.app.state.approvals.store.allow(r.json()["error"]["id"])
    assert c.post("/anthropic/v1/messages", json=_body("anthropic")).status_code == 200

async def test_tty_send_releases_and_remembers(tmp_path, analyzer):
    class J:
        def is_sensitive(self, t): return "<PHONE_NUMBER_" in t
    class P:
        available = True; asked = 0
        async def ask(self, item): self.asked += 1; return "send"
    p = P(); cfg, c = _app(tmp_path, analyzer, Upstream(), judge=J(), prompter=p)
    assert c.post("/anthropic/v1/messages", json=_body("anthropic")).status_code == 200
    assert c.post("/anthropic/v1/messages", json=_body("anthropic")).status_code == 200
    assert p.asked == 1

def test_judge_exception_fails_closed(tmp_path, analyzer):
    class J:
        def is_sensitive(self, t): raise RuntimeError("down")
    cfg, c = _app(tmp_path, analyzer, Upstream(), judge=J())
    assert c.post("/anthropic/v1/messages", json=_body("anthropic")).status_code == 422

def test_observe_mode_forwards_unchanged_and_logs(tmp_path, analyzer):
    up = Upstream(); cfg, c = _app(tmp_path, analyzer, up, mode="observe")
    assert c.post("/anthropic/v1/messages", json=_body("anthropic")).status_code == 200
    assert "555-010-0199" in up.seen[0].content.decode()
    assert json.loads((tmp_path / "receipt.jsonl").read_text())["replaced"]["PHONE_NUMBER"] == 1

def test_upstream_error_forwarded_and_unreachable_is_502(tmp_path, analyzer):
    cfg, c = _app(tmp_path, analyzer, Upstream(status=429, json_reply={"error": "slow down"}))
    assert c.post("/anthropic/v1/messages", json=_body("anthropic")).status_code == 429
    cfg2, c2 = _app(tmp_path / "b", analyzer, Upstream(raise_=httpx.ConnectError("refused")))
    r = c2.post("/anthropic/v1/messages", json=_body("anthropic"))
    assert r.status_code == 502 and r.json()["error"]["type"] == "hermie_upstream_unreachable"

def test_non_json_is_passthrough_only_on_allowlist(tmp_path, analyzer):
    up = Upstream(json_reply={"data": []}); cfg, c = _app(tmp_path, analyzer, up)
    assert c.get("/openai/v1/models").status_code == 200
    r = c.post("/anthropic/v1/messages", content=b"not json", headers={"content-type": "text/plain"})
    assert r.status_code == 415 and len(up.seen) == 1

async def test_large_tool_result_does_not_block_the_loop(tmp_path, analyzer):
    import asyncio
    body = _body("anthropic"); body["messages"][2]["content"][0]["content"] = "log\n" + ("line 555-010-0199\n" + "build output line\n" * 160) * 100
    cfg, _ = _app(tmp_path, analyzer, Upstream())       # ~290 KB, under spaCy's 1M-char limit, so the scan really runs
    app = _.app
    ticks = 0
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://h") as client:
        req = asyncio.create_task(client.post("/anthropic/v1/messages", json=body))
        while not req.done():
            await asyncio.sleep(0.01); ticks += 1
        r = await req
    assert r.status_code == 200 and ticks >= 5            # Review Focus 1: the loop kept running during the scan
    line = json.loads((tmp_path / "receipt.jsonl").read_text().splitlines()[-1])
    assert any(p["origin"] == "tool" and "PHONE_NUMBER" in p["entities"] for p in line["new_parts"])

def test_send_upstream_refuses_plain_bytes():
    with pytest.raises(TypeError):
        import asyncio
        asyncio.run(server.send_upstream(httpx.AsyncClient(), "POST", "https://x.test", {}, b"{}", False))

def test_no_other_send_call_in_the_package():
    import re, pathlib
    hits = [p for p in pathlib.Path("hermie").rglob("*.py") if p.name != "server.py" and p.name != "judge.py"
            and re.search(r"\.(send|stream|post|request)\(", p.read_text())]
    assert hits == [], hits


def test_only_allowlisted_headers_reach_the_upstream(tmp_path, analyzer):
    up = Upstream(); cfg, c = _app(tmp_path, analyzer, up)
    r = c.post("/anthropic/v1/messages", json=_body("anthropic"),
               headers={"authorization": "Bearer t", "x-stainless-os": "MacOS", "cookie": "a=b", "x-secret-thing": "1"})
    h = up.seen[0].headers
    assert h["authorization"] == "Bearer t" and h["x-stainless-os"] == "MacOS"
    assert "cookie" not in h and "x-secret-thing" not in h
    assert str(up.seen[0].url) == "https://api.anthropic.com/v1/messages"
    assert len(r.headers["x-hermie-request-id"]) == 12


def test_gemini_method_call_under_models_is_not_passthrough(tmp_path, analyzer):
    up = Upstream(json_reply={"candidates": []}); cfg, c = _app(tmp_path, analyzer, up)
    assert c.post("/gemini/v1beta/models/gemini-2.5-pro:generateContent", json=_body("gemini")).status_code == 200
    assert not any(v in up.seen[0].content.decode() for v in REAL)
    assert server.is_passthrough("/gemini/v1beta/models") and server.is_passthrough("/openai/v1/models/gpt-5")
    assert not server.is_passthrough("/openai/v1/modelsx")


def test_mapping_unwritable_is_507_and_every_path_writes_a_receipt(tmp_path, analyzer):
    from hermie.gate.redact import MappingStoreError
    up = Upstream(); cfg, c = _app(tmp_path, analyzer, up)
    def broken(fn): raise MappingStoreError("read-only")
    c.app.state.gate.store.update = broken
    r = c.post("/anthropic/v1/messages", json=_body("anthropic"))
    assert r.status_code == 507 and r.json()["error"]["type"] == "hermie_mapping_unwritable" and up.seen == []
    c.post("/anthropic/v1/messages", content=b"x", headers={"content-type": "text/plain"})
    c.post("/nowhere/v1/x", json={})
    lines = [json.loads(l) for l in (tmp_path / "receipt.jsonl").read_text().splitlines()]
    assert [l["status"] for l in lines] == [507, 415, 404]


def test_streaming_closes_the_upstream_response(tmp_path, analyzer):
    closed = []
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self): yield (FIX / "streams" / "anthropic.sse").read_bytes()
        async def aclose(self): closed.append(True)
    def up(req): return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Body())
    cfg, c = _app(tmp_path, analyzer, up)
    r = c.post("/anthropic/v1/messages", json=_body("anthropic") | {"stream": True})
    assert r.status_code == 200 and closed == [True]


def _receipts(tmp_path): return [json.loads(l) for l in (tmp_path / "receipt.jsonl").read_text().splitlines()]


def test_dot_segments_and_encoded_slashes_are_refused(tmp_path, analyzer):
    up = Upstream(); cfg, c = _app(tmp_path, analyzer, up)
    for p in ("/openai/v1/models/..%2Fchat%2Fcompletions", "/anthropic/v1/models/..%2Fmessages",
              "/openai/v1/models/%2E%2E/chat/completions"):
        r = c.post(p, json=_body("openai_chat"))
        assert r.status_code == 400 and r.json()["error"]["type"] == "hermie_bad_path", p
    assert up.seen == []


def test_passthrough_is_bodyless_get_only_and_files_are_refused(tmp_path, analyzer):
    up = Upstream(json_reply={"data": []}); cfg, c = _app(tmp_path, analyzer, up)
    assert c.post("/openai/v1/models", json={"messages": [{"role": "user", "content": "555-010-0199"}]}).status_code == 415
    for method in ("GET", "POST"):
        r = c.request(method, "/openai/v1/files", content=b"" if method == "GET" else b"file bytes 555-010-0199")
        assert r.status_code == 415 and r.json()["error"]["message"] == "hermie does not proxy file uploads"
    assert up.seen == []
    assert c.get("/openai/v1/models").status_code == 200 and up.seen[0].content == b""
    assert _receipts(tmp_path)[-1]["mode"] == "passthrough"
    assert not list((tmp_path / "outbound").glob("*.json")) if (tmp_path / "outbound").exists() else True


def test_new_parts_only_for_leaves_not_seen_before(tmp_path, analyzer):
    cfg, c = _app(tmp_path, analyzer, Upstream())
    c.post("/anthropic/v1/messages", json=_body("anthropic"))
    c.post("/anthropic/v1/messages", json=_body("anthropic"))
    first, second = _receipts(tmp_path)
    assert first["new_parts"] and second["new_parts"] == []
    assert {"origin": "tool", "tool": "Read", "size": len(".env\nAWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"),
            "entities": ["SECRET"]} in first["new_parts"]


def test_approved_by_session_user_and_allowed(tmp_path, analyzer):
    class J:
        def is_sensitive(self, t): return "<PHONE_NUMBER_" in t
    class P:
        available = True
        def __init__(self, choice): self.choice = choice
        async def ask(self, item): return self.choice
    for choice, who in (("allow_all", "session"), ("send", "user")):
        d = tmp_path / choice
        cfg, c = _app(d, analyzer, Upstream(), judge=J(), prompter=P(choice))
        assert c.post("/anthropic/v1/messages", json=_body("anthropic")).status_code == 200
        assert _receipts(d)[-1]["approved_by"] == who and _receipts(d)[-1]["held"]
    d = tmp_path / "stored"
    cfg, c = _app(d, analyzer, Upstream(), judge=J())
    c.app.state.approvals.store.allow(c.post("/anthropic/v1/messages", json=_body("anthropic")).json()["error"]["id"])
    assert c.post("/anthropic/v1/messages", json=_body("anthropic")).status_code == 200
    assert _receipts(d)[-1]["approved_by"] == "allowed"


def test_custom_route_without_upstream_is_404_and_with_one_forwards(tmp_path, analyzer):
    up = Upstream(); cfg, c = _app(tmp_path, analyzer, up)
    assert c.post("/custom/v1/messages", json=_body("anthropic")).status_code == 404 and up.seen == []
    cfg, c = _app(tmp_path / "b", analyzer, up, custom_upstream="https://llm.internal.test/api")
    assert c.post("/custom/v1/messages", json=_body("anthropic")).status_code == 200
    assert str(up.seen[0].url) == "https://llm.internal.test/api/v1/messages"


class _ChunkedSSE(httpx.AsyncByteStream):
    def __init__(self): self.closed = False
    async def __aiter__(self):
        for block in (FIX / "streams" / "anthropic.sse").read_bytes().split(b"\n\n"):
            yield block + b"\n\n"
    async def aclose(self): self.closed = True


async def test_partial_stream_read_still_writes_receipt_and_closes_upstream(tmp_path, analyzer):
    body = _ChunkedSSE()
    cfg, c = _app(tmp_path, analyzer, lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(c.app), base_url="http://h") as client:
        async with client.stream("POST", "/anthropic/v1/messages", json=_body("anthropic") | {"stream": True}) as r:
            async for _ in r.aiter_bytes():
                break
    assert body.closed and _receipts(tmp_path)[-1]["stream"] is True


def _raw_scope(payload):
    return {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "http_version": "1.1", "method": "POST",
            "scheme": "http", "path": "/anthropic/v1/messages", "raw_path": b"/anthropic/v1/messages", "query_string": b"",
            "headers": [(b"content-type", b"application/json")], "client": ("127.0.0.1", 1), "server": ("h", 80)}


async def test_client_disconnect_before_first_byte_writes_one_receipt_and_closes_upstream(tmp_path, analyzer):
    from starlette.requests import ClientDisconnect
    body = _ChunkedSSE()
    cfg, c = _app(tmp_path, analyzer, lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body))
    payload = json.dumps(_body("anthropic") | {"stream": True}).encode()
    async def receive(): return {"type": "http.request", "body": payload, "more_body": False}
    async def send(msg):
        if msg["type"] == "http.response.start":
            raise OSError("client went away")
    try:
        await c.app(_raw_scope(payload), receive, send)
    except (ClientDisconnect, OSError):
        pass
    lines = _receipts(tmp_path)
    assert body.closed and len(lines) == 1 and lines[0]["status"] == 200 and lines[0]["unrestored"] == 0


def test_normal_stream_writes_exactly_one_receipt(tmp_path, analyzer):
    body = _ChunkedSSE()
    cfg, c = _app(tmp_path, analyzer, lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body))
    assert c.post("/anthropic/v1/messages", json=_body("anthropic") | {"stream": True}).status_code == 200
    assert body.closed and len(_receipts(tmp_path)) == 1


async def test_client_disconnect_mid_stream_writes_receipt_and_closes_upstream(tmp_path, analyzer):
    body = _ChunkedSSE()
    cfg, c = _app(tmp_path, analyzer, lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body))
    payload = json.dumps(_body("anthropic") | {"stream": True}).encode()
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "http_version": "1.1", "method": "POST",
             "scheme": "http", "path": "/anthropic/v1/messages", "raw_path": b"/anthropic/v1/messages", "query_string": b"",
             "headers": [(b"content-type", b"application/json")], "client": ("127.0.0.1", 1), "server": ("h", 80)}
    sent = []
    async def receive(): return {"type": "http.request", "body": payload, "more_body": False}
    async def send(msg):
        if msg["type"] == "http.response.body" and msg.get("body"):
            if sent:
                raise OSError("client went away")
            sent.append(msg)
    try:
        await c.app(scope, receive, send)
    except Exception:
        pass
    assert sent and body.closed and len(_receipts(tmp_path)) == 1 and _receipts(tmp_path)[0]["status"] == 200


# --- final fix wave ---

def test_known_values_resent_next_turn_map_back(tmp_path, analyzer):
    """C1: values restored into the client's history come back as the turn-1 placeholders, in assistant text,
    tool_use input and tool_result alike."""
    up = Upstream(); cfg, c = _app(tmp_path, analyzer, up)
    user1 = "password = Hunter2Secret99 for the db. I am John Smith, call me at 555-010-0199."
    turn1 = {"model": "m", "max_tokens": 100, "messages": [{"role": "user", "content": user1}]}
    assert c.post("/anthropic/v1/messages", json=turn1).status_code == 200
    mapping = MappingStore(cfg.mapping_path).mapping
    assert "Hunter2Secret99" in mapping.values() and "555-010-0199" in mapping.values()
    turn2 = {"model": "m", "max_tokens": 100, "messages": [
        {"role": "user", "content": user1},
        {"role": "assistant", "content": [
            {"type": "text", "text": "Thanks John Smith. Run mysql -pHunter2Secret99 and I will call 555-010-0199."},
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "mysql -u root -pHunter2Secret99"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "logged in with Hunter2Secret99"}]}]}
    assert c.post("/anthropic/v1/messages", json=turn2).status_code == 200
    sent = up.seen[1].content.decode()
    for value in mapping.values():
        assert value not in sent, value
    assert MappingStore(cfg.mapping_path).mapping == mapping          # the turn-1 placeholders, nothing new
    secret = next(k for k, v in mapping.items() if v == "Hunter2Secret99")
    assert f"mysql -u root -p{secret}" in sent and f"logged in with {secret}" in sent


def test_unscannable_user_message_is_rejected_without_offering_send(tmp_path):
    """C2(b): a user message whose scan failed is rejected; the prompt is never shown, allow does not release it."""
    class Raising:
        def analyze(self, **kw): raise RuntimeError("down")
    class P:
        available = True; asked = 0
        async def ask(self, item): self.asked += 1; return "send"
    cfg = Config(data_dir=tmp_path)
    gate = Gate(cfg, analyzer=Raising(), store=MappingStore(cfg.mapping_path))
    up = Upstream(); p = P()
    c = TestClient(server.create_app(cfg, gate=gate, upstream_client=httpx.AsyncClient(transport=httpx.MockTransport(up)), prompter=p))
    body = {"model": "m", "messages": [{"role": "user", "content": "call 555-010-0199"}]}
    r = c.post("/anthropic/v1/messages", json=body)
    assert r.status_code == 422 and "could not be scanned" in r.json()["error"]["message"] and p.asked == 0
    c.app.state.approvals.store.allow(gate.scan("call 555-010-0199", Origin.USER).hash)
    c.app.state.approvals.session.all = True
    assert c.post("/anthropic/v1/messages", json=body).status_code == 422 and up.seen == []
