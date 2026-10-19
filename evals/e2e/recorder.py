"""Recording forward proxy: the independent witness of what left the machine. Every request is forwarded to the
upstream unchanged and its body is written to OUT/NNN.json (NNN from 001) with OUT/NNN.meta.json (method, path,
status, time). Headers are forwarded but never written: they hold the client's credentials. Responses, streams
included, go back chunk by chunk as they arrive.
"""
from __future__ import annotations

import contextlib
import json
import threading
import time
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.routing import Route

HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
              "transfer-encoding", "upgrade", "host", "content-length"}
METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def create_recorder(upstream: str, out_dir: Path, client: httpx.AsyncClient | None = None) -> Starlette:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=30.0))
    lock = threading.Lock()
    counter = [len(list(out_dir.glob("*.meta.json")))]

    async def forward(request: Request):
        body = await request.body()
        with lock:
            counter[0] += 1
            n = f"{counter[0]:03d}"
        (out_dir / f"{n}.json").write_bytes(body)
        meta = {"method": request.method, "path": request.url.path, "time": time.time(), "status": None}
        url = upstream.rstrip("/") + request.url.path + (f"?{request.url.query}" if request.url.query else "")
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}
        try:
            resp = await client.send(client.build_request(request.method, url, headers=headers, content=body),
                                     stream=True)
        except httpx.HTTPError as e:
            meta["status"] = 502
            meta["error"] = type(e).__name__
            (out_dir / f"{n}.meta.json").write_text(json.dumps(meta))
            return StreamingResponse(iter([json.dumps({"error": "recorder_upstream_error"}).encode()]),
                                     status_code=502, media_type="application/json")
        meta["status"] = resp.status_code
        (out_dir / f"{n}.meta.json").write_text(json.dumps(meta))
        out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in HOP_BY_HOP
                       and k.lower() != "content-encoding"}
        # decoded bytes, so content-encoding is dropped; chunks still go out as they arrive
        return StreamingResponse(resp.aiter_bytes(), status_code=resp.status_code, headers=out_headers,
                                 background=BackgroundTask(resp.aclose))

    return Starlette(routes=[Route("/{path:path}", forward, methods=METHODS)])


@contextlib.contextmanager
def serve_recorder(upstream: str, out_dir: Path, port: int, client: httpx.AsyncClient | None = None):
    """Run the recorder on 127.0.0.1:port in a thread for the duration of the block."""
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(create_recorder(upstream, out_dir, client), host="127.0.0.1", port=port,
                                           log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    t0 = time.monotonic()
    while not server.started:
        if not thread.is_alive() or time.monotonic() - t0 > 10:
            raise RuntimeError(f"recorder did not start on port {port}")
        time.sleep(0.02)
    try:
        yield server
    finally:
        server.should_exit = True
        thread.join(10)
