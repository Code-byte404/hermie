import httpx, pytest
from hermie.config import Config
from hermie.gate.judge import ContextualJudge, JudgeError, CONTEXTUAL_PRIVACY_QUESTION

def _judge(handler):
    cfg = Config(judge="ollama:fake", data_dir=None)
    return ContextualJudge(cfg, client=httpx.Client(transport=httpx.MockTransport(handler)))

def test_yes_is_sensitive_and_sends_think_false():
    seen = {}
    def h(req):
        seen.update(req.extensions | {"json": __import__("json").loads(req.content)})
        return httpx.Response(200, json={"message": {"content": '{"answer": "yes"}'}})
    j = _judge(h)
    assert j.is_sensitive("the layoff list for Q4") is True
    body = seen["json"]
    assert body["think"] is False and body["model"] == "fake" and CONTEXTUAL_PRIVACY_QUESTION in body["messages"][0]["content"]

def test_no_is_clean():
    j = _judge(lambda r: httpx.Response(200, json={"message": {"content": '{"answer": "no"}'}}))
    assert j.is_sensitive("how do I sort a list") is False

def test_errors_propagate_as_judge_error():
    with pytest.raises(JudgeError):
        _judge(lambda r: httpx.Response(500, text="boom")).is_sensitive("x")
    with pytest.raises(JudgeError):
        _judge(lambda r: httpx.Response(200, json={"message": {"content": "maybe"}})).is_sensitive("x")
