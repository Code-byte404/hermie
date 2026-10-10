import json

import httpx

from hermie.config import Config
from hermie.gate.local import LOCAL_PREFIX, LocalRewriter, make_local


def _rewriter(handler, **kw):
    cfg = Config(data_dir=None, local_model="ollama:m", **kw)
    return LocalRewriter(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _ok(content):
    return lambda req: httpx.Response(200, json={"message": {"content": content}})


def test_make_local_is_none_without_a_model():
    assert make_local(Config(data_dir=None)) is None
    assert isinstance(make_local(Config(data_dir=None, local_model="ollama:m")), LocalRewriter)


def test_prefix_is_plain_text():
    assert LOCAL_PREFIX.startswith("[rewritten locally]")


async def test_rewrite_returns_stripped_model_text_and_sends_think_false():
    seen = []
    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={"message": {"content": "  a plain summary \n"}})
    out = await _rewriter(handler).rewrite("the layoff list is final")
    assert out == "a plain summary"
    body = seen[0]
    assert body["model"] == "m" and body["think"] is False and body["stream"] is False
    assert body["messages"][-1] == {"role": "user", "content": "the layoff list is final"}
    assert "never repeat" in body["messages"][0]["content"].lower()


async def test_rewrite_none_on_empty_output():
    assert await _rewriter(_ok("   ")).rewrite("x") is None


async def test_rewrite_none_on_http_error_and_bad_json():
    assert await _rewriter(lambda r: httpx.Response(500)).rewrite("x") is None
    assert await _rewriter(lambda r: httpx.Response(200, json={"oops": 1})).rewrite("x") is None
    def boom(req): raise httpx.ConnectError("down")
    assert await _rewriter(boom).rewrite("x") is None


async def test_rewrite_none_for_text_over_the_limit_without_calling_the_model():
    calls = []
    def handler(req): calls.append(1); return httpx.Response(200, json={"message": {"content": "x"}})
    assert await _rewriter(handler, local_max_chars=10).rewrite("y" * 11) is None
    assert calls == []
