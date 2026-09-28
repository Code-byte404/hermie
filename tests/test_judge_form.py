"""Batched judge questions: the routing stage asks its three questions in one structured call (sampled JUDGE_SAMPLES
times) instead of one call each; the privacy gate's contextual question stays a request of its own."""
from __future__ import annotations

import json

import httpx
import pytest

from hermie.config import Settings
from hermie.judge import ChoiceAnswer, OllamaJudge, ScoreAnswer, as_score, form_via_primitives, to_choice, to_score
from hermie.policy import COMPLEXITY_LEVELS, NEEDS_WORKSPACE_QUESTION, TASK_TYPES, Force
from hermie.privacy import CONTEXTUAL_PRIVACY_QUESTION, PrivacyGate
from hermie.router import EntryRouter

from .conftest import FakeJudge

QUESTIONS = {
    "task": ("What kind of task is this request?", TASK_TYPES),
    "complexity": ("How difficult is it?", {f"L{i}": d for i, d in enumerate(COMPLEXITY_LEVELS)}),
    "needs_workspace": (NEEDS_WORKSPACE_QUESTION, {"yes": "yes", "no": "no"}),
}


def _judge(answers_by_seed: dict[int, dict], samples: int, requests: list) -> OllamaJudge:
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        requests.append(body)
        ans = answers_by_seed[body["options"]["seed"]]
        return httpx.Response(200, json={"message": {"content": json.dumps(ans)},
                                         "prompt_eval_count": 10, "eval_count": 5})
    s = Settings(judge_samples=samples, ollama_url="http://ollama.test")
    return OllamaJudge(s, client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_ollama_form_asks_every_question_in_one_request_per_sample():
    reqs: list = []
    judge = _judge({0: {"task": "planning", "complexity": "L2", "needs_workspace": "yes"},
                    1: {"task": "planning", "complexity": "L1", "needs_workspace": "yes"},
                    2: {"task": "complex", "complexity": "L2", "needs_workspace": "no"}}, samples=3, requests=reqs)
    ans = judge.form("Build me a CRM", QUESTIONS)
    assert len(reqs) == 3
    schema = reqs[0]["format"]
    assert set(schema["properties"]) == {"task", "complexity", "needs_workspace"}
    assert schema["properties"]["task"]["enum"] == list(TASK_TYPES)
    assert set(schema["required"]) == {"task", "complexity", "needs_workspace"}
    user = reqs[0]["messages"][1]["content"]
    assert "Build me a CRM" in user and NEEDS_WORKSPACE_QUESTION in user and "planning:" in user
    assert ans["task"].choice == "planning" and ans["task"].confidence == pytest.approx(2 / 3)
    assert ans["task"].probabilities["complex"] == pytest.approx(1 / 3) and ans["task"].probabilities["simple"] == 0.0
    assert ans["complexity"].choice == "L2" and ans["complexity"].probabilities["L2"] == pytest.approx(2 / 3)
    assert ans["needs_workspace"].probabilities["yes"] == pytest.approx(2 / 3)


def test_ollama_form_rejects_out_of_range_answers():
    judge = _judge({0: {"task": "weird", "complexity": "L0", "needs_workspace": "no"}}, samples=1, requests=[])
    with pytest.raises(ValueError):
        judge.form("x", QUESTIONS)


def test_ollama_form_reports_usage_once_per_sample():
    reqs: list = []
    judge = _judge({0: {"task": "simple", "complexity": "L0", "needs_workspace": "no"}}, samples=1, requests=reqs)
    seen = []
    judge.usage_sink = lambda p, c: seen.append((p, c))
    judge.form("x", QUESTIONS)
    assert seen == [(10, 5)]


def test_to_choice_and_to_score_match_single_question_semantics():
    c = to_choice({"a": 0.2, "b": 0.8}, samples=3)
    assert c == ChoiceAnswer("b", {"a": 0.2, "b": 0.8}, 0.8)
    assert to_choice({"a": 1.0, "b": 0.0}, samples=1).confidence == 0.5  # one sample carries no confidence
    sc = to_score({"L0": 0.0, "L1": 1.0, "L2": 0.0}, samples=3)
    assert sc == ScoreAnswer(1, [0.0, 1.0, 0.0], 1.0)
    assert as_score(ChoiceAnswer("L2", {"L0": 0.1, "L1": 0.2, "L2": 0.7}, 0.7)) == ScoreAnswer(2, [0.1, 0.2, 0.7], 0.7)


class _PrimitivesOnlyJudge:
    """A judge implementing only the three Jev primitives (no form): the router must still work."""

    def __init__(self):
        self.calls = []

    def choice(self, state, instructions, options):
        self.calls.append(instructions)
        return ChoiceAnswer("repetitive", {k: (1.0 if k == "repetitive" else 0.0) for k in options}, 1.0)

    def score(self, state, instructions, levels):
        self.calls.append(instructions)
        return ScoreAnswer(0, [1.0, 0.0, 0.0], 1.0)

    def noul(self, state, statement):
        self.calls.append(statement)
        return 0.0


def test_form_via_primitives_delegates_each_question():
    j = _PrimitivesOnlyJudge()
    ans = form_via_primitives(j, "x", {**QUESTIONS, "privacy": (CONTEXTUAL_PRIVACY_QUESTION, {"yes": "yes", "no": "no"})})
    assert ans["task"] == ChoiceAnswer("repetitive", {k: (1.0 if k == "repetitive" else 0.0) for k in TASK_TYPES}, 1.0)
    assert ans["complexity"] == ChoiceAnswer("L0", {"L0": 1.0, "L1": 0.0, "L2": 0.0}, 1.0)  # keeps the judge's confidence
    assert ans["needs_workspace"].probabilities["yes"] == 0.0 and ans["privacy"].choice == "no"
    assert len(j.calls) == 4


# ---------------------------------------------------------------- router

def _router(settings, analyzer, judge):
    gate = PrivacyGate(settings, judge=judge, analyzer=analyzer)
    return EntryRouter(settings, judge, gate, None)


async def test_router_asks_the_judge_once_with_all_questions(settings, analyzer):
    judge = FakeJudge(task="planning", needs_ws=True)
    r = await _router(settings, analyzer, judge).route("Design a CRM", "Design a CRM")
    assert judge.form_calls == 1
    # needs_workspace answered inside the form; the contextual privacy question is its own request (gate.check)
    assert sorted(q for q, _ in judge.calls) == sorted([NEEDS_WORKSPACE_QUESTION, CONTEXTUAL_PRIVACY_QUESTION])
    assert r.decision.route.value == "plan"
    assert r.signals.task.confidence == 0.9  # the judge's own confidence, not recomputed from a single sample
    assert r.verdict.sensitive is False and r.verdict.contextual_prob == 0.0
    assert r.signals.needs_workspace_prob == 1.0


async def test_router_batched_privacy_still_fails_closed_on_rules_hits(settings, analyzer):
    judge = FakeJudge(task="planning")
    r = await _router(settings, analyzer, judge).route("call 13812345678", "call 13812345678")
    assert r.verdict.sensitive and "CN_MOBILE" in r.verdict.reason
    assert r.decision.route.value == "plan"  # private + wants cloud = plan mode


async def test_router_batched_contextual_privacy_marks_sensitive(settings, analyzer):
    judge = FakeJudge(task="simple", secrets=("layoffs",))
    r = await _router(settings, analyzer, judge).route("plan the layoffs", "plan the layoffs")
    assert r.verdict.sensitive and r.verdict.reason.startswith("contextual") and r.verdict.contextual_prob == 1.0
    assert r.decision.route.value == "local"


async def test_router_form_failure_routes_local(settings, analyzer):
    judge = FakeJudge(task="planning", fail_task=True)
    r = await _router(settings, analyzer, judge).route("Design a CRM", "Design a CRM")
    assert r.decision.route.value == "local" and r.signals is None


async def test_router_privacy_check_failure_is_sensitive(settings, analyzer):
    judge = FakeJudge(task="planning", fail_privacy=True)
    r = await _router(settings, analyzer, judge).route("Design a CRM", "Design a CRM")
    assert r.verdict.sensitive and r.verdict.reason.startswith("judge_error")
    assert r.decision.route.value in ("local", "plan") and not r.verdict.contextual_prob


async def test_router_uses_primitives_when_judge_has_no_form(settings, analyzer):
    judge = _PrimitivesOnlyJudge()
    r = await _router(settings, analyzer, judge).route("Rename these files", "Rename these files")
    assert r.decision.route.value == "local" and r.signals.task.confidence == 1.0
    assert CONTEXTUAL_PRIVACY_QUESTION in judge.calls and NEEDS_WORKSPACE_QUESTION in judge.calls


async def test_router_judge_batch_off_uses_primitives(settings, analyzer):
    settings.judge_batch = False
    judge = FakeJudge(task="planning")
    r = await _router(settings, analyzer, judge).route("Design a CRM", "Design a CRM")
    assert judge.form_calls == 0 and len(judge.calls) == 2  # needs_workspace + contextual privacy via noul
    assert r.decision.route.value == "plan"
