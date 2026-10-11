import json

import httpx

from hermie.config import Config
from hermie.gate.local import LocalRewriter, make_local


def _rewriter(handler, **kw):
    cfg = Config(data_dir=None, local_model="ollama:m", **kw)
    return LocalRewriter(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _reply(spans):
    return lambda req: httpx.Response(200, json={"message": {"content": json.dumps({"spans": spans})}})


def test_make_local_is_none_without_a_model():
    assert make_local(Config(data_dir=None)) is None
    assert isinstance(make_local(Config(data_dir=None, local_model="ollama:m")), LocalRewriter)


async def test_spans_returns_pairs_and_sends_the_request_shape():
    seen = []
    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={"message": {"content": json.dumps(
            {"spans": [{"text": "Lin", "type": "PERSON"}, {"text": "62%", "type": "NUMBER"}]})}})
    out = await _rewriter(handler).spans("Lin is at 62% of plan")
    assert out == [("Lin", "PERSON"), ("62%", "NUMBER")]
    body = seen[0]
    assert body["model"] == "m" and body["think"] is False and body["stream"] is False
    assert body["messages"][-1] == {"role": "user", "content": "<message>Lin is at 62% of plan</message>"}
    assert "do not answer it" in body["messages"][0]["content"].lower() and "spans" in json.dumps(body["format"])


async def test_spans_keep_only_literal_substrings_with_known_types_and_a_sane_length():
    rows = [{"text": "Lin", "type": "PERSON"}, {"text": "Lin", "type": "ORGANIZATION"},   # duplicate: first wins
            {"text": "Mia", "type": "PERSON"},            # not in the text
            {"text": "a", "type": "PERSON"},               # too short
            {"text": "plan", "type": "BOGUS"},             # unknown type
            {"text": "x" * 201, "type": "FACT"}]           # too long
    assert await _rewriter(_reply(rows)).spans("Lin is at plan " + "x" * 201) == [("Lin", "PERSON")]


async def test_spans_none_when_nothing_usable():
    assert await _rewriter(_reply([])).spans("x y") is None
    assert await _rewriter(_reply([{"text": "zzz", "type": "PERSON"}])).spans("x y") is None


async def test_spans_none_on_http_error_and_bad_json():
    assert await _rewriter(lambda r: httpx.Response(500)).spans("x") is None
    assert await _rewriter(lambda r: httpx.Response(200, json={"oops": 1})).spans("x") is None
    assert await _rewriter(lambda r: httpx.Response(200, json={"message": {"content": "not json"}})).spans("x") is None
    def boom(req): raise httpx.ConnectError("down")
    assert await _rewriter(boom).spans("x") is None


async def test_spans_none_for_text_over_the_limit_without_calling_the_model():
    calls = []
    def handler(req): calls.append(1); return httpx.Response(200, json={"message": {"content": "{}"}})
    assert await _rewriter(handler, local_max_chars=10).spans("y" * 11) is None
    assert calls == []
