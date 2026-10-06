# tests/test_server.py
import json, httpx, pytest
from pathlib import Path
from starlette.testclient import TestClient
from hermie.config import Config
from hermie.gate.gate import Gate
from hermie.gate.redact import MappingStore
from hermie.gate.types import CleanBody
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

def test_large_tool_result_does_not_block_the_loop(tmp_path, analyzer):
    body = _body("anthropic"); body["messages"][2]["content"][0]["content"] = "log\n" + ("line 555-010-0199\n" * 60_000)
    cfg, c = _app(tmp_path, analyzer, Upstream())
    assert c.post("/anthropic/v1/messages", json=body).status_code == 200      # Review Focus 1: finishes, scanned in a thread

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
