import httpx, pytest
from pathlib import Path
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


# --- the optional Laya backend (a fine-tuned decision model through laya-mlx) ---

import sys
import types

import pytest

from hermie.gate.judge import LAYA_QUESTION, LayaJudge, make_judge


class _FakeAgent:
    def __init__(self, scorer):
        self.calls = []
        self.scorer = scorer

    def predict(self, text, questions):
        self.calls.append((text, questions))
        return {"answers": {"private": {"type": "noul", "noul": self.scorer(text)}}}


def _fake_laya(monkeypatch, scorer=lambda t: 0.9 if "SECRET" in t else 0.1, load_error=None):
    agent = _FakeAgent(scorer)
    mod = types.ModuleType("laya_mlx")

    def load(path):
        if load_error:
            raise load_error
        mod.loaded = path
        return agent
    mod.load = load
    monkeypatch.setitem(sys.modules, "laya_mlx", mod)
    return agent, mod


def test_make_judge_picks_the_backend_by_prefix(monkeypatch):
    agent, mod = _fake_laya(monkeypatch)
    assert isinstance(make_judge(Config(judge="ollama:m", data_dir=None)), ContextualJudge)
    j = make_judge(Config(judge="laya:/models/x", data_dir=None))
    assert isinstance(j, LayaJudge) and mod.loaded == "/models/x"
    with pytest.raises(ValueError, match="expected 'ollama:MODEL' or 'laya:PATH'"):
        make_judge(Config(judge="gpt:4", data_dir=None))


def test_laya_judge_scores_with_the_training_question_and_threshold(monkeypatch):
    agent, _ = _fake_laya(monkeypatch)
    j = LayaJudge(Config(judge="laya:/m", judge_threshold=0.5, data_dir=None))
    assert j.probability("a SECRET plan") == 0.9 and j.is_sensitive("a SECRET plan") and not j.is_sensitive("def f(): pass")
    q = agent.calls[0][1]["private"]
    assert q == {"type": "noul", "instructions": LAYA_QUESTION}


def test_laya_question_is_the_one_the_models_were_trained_on():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals" / "judge_data"))
    import taxonomy
    assert LAYA_QUESTION == taxonomy.QUESTION


def test_laya_judge_takes_the_maximum_over_windows_and_caps_the_text(monkeypatch):
    agent, _ = _fake_laya(monkeypatch)
    j = LayaJudge(Config(judge="laya:/m", data_dir=None))
    text = "x" * 9000 + "SECRET" + "y" * 5000          # the private part sits in the third window
    assert j.probability(text) == 0.9
    assert all(len(c[0]) <= LayaJudge.WINDOW for c in agent.calls)
    agent.calls.clear()
    j.probability("z" * 200_000)
    assert len(agent.calls) == LayaJudge.MAX_WINDOWS     # bounded latency on huge texts


def test_laya_judge_failures_are_judge_errors_and_a_missing_package_is_a_clear_startup_error(monkeypatch):
    def boom(_):
        raise RuntimeError("metal failed")
    _fake_laya(monkeypatch, scorer=boom)
    with pytest.raises(JudgeError):
        LayaJudge(Config(judge="laya:/m", data_dir=None)).probability("anything")
    _fake_laya(monkeypatch, scorer=lambda t: "nan")
    with pytest.raises(JudgeError):
        LayaJudge(Config(judge="laya:/m", data_dir=None)).probability("anything")
    monkeypatch.setitem(sys.modules, "laya_mlx", None)     # import fails
    with pytest.raises(ValueError, match="laya-mlx"):
        LayaJudge(Config(judge="laya:/m", data_dir=None))
    _fake_laya(monkeypatch, load_error=OSError("no such checkpoint"))
    with pytest.raises(ValueError, match="could not load"):
        LayaJudge(Config(judge="laya:/nope", data_dir=None))


def test_laya_path_expands_the_home_directory(monkeypatch):
    _, mod = _fake_laya(monkeypatch)
    LayaJudge(Config(judge="laya:~/models/x", data_dir=None))
    assert mod.loaded == str(Path("~/models/x").expanduser()) and not mod.loaded.startswith("~")
