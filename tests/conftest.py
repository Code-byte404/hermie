"""Test fixtures: fake judge model + FunctionModel fake LLM. No Ollama / DeepSeek / GPU needed, but the real
Presidio (needs zh_core_web_sm) and the real Seatbelt sandbox are used."""
from __future__ import annotations

import json
import math
from typing import Callable

import pytest
from pydantic_ai.messages import (ModelRequest, ModelResponse, RetryPromptPart, TextPart, ToolCallPart, ToolReturnPart,
                                  UserPromptPart)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from hermie.agents import SMUGGLE_QUESTION, ModelFactory
from hermie.capabilities import RISK_QUESTION, STUCK_QUESTION
from hermie.config import RunMode, Settings
from hermie.connectors.base import ConnectorResult, ConnectorTool, Status
from hermie.core import Hermie
from hermie.judge import ChoiceAnswer, ScoreAnswer, form_via_primitives
from hermie.policy import BUSINESS_DATA_QUESTION, NEEDS_WORKSPACE_QUESTION
from hermie.privacy import CONTEXTUAL_PRIVACY_QUESTION, build_analyzer


class FakeJudge:
    """task/cx drive routing; ctx_priv drives contextual privacy; any word in secrets appearing marks the text as
    contextually sensitive."""

    def __init__(self, task="simple", conf=0.9, cx=0, cx_conf=0.9, needs_ws=True, verify=0.9,
                 secrets=(), fail_privacy=False, fail_task=False, risk=0, business=False):
        self.task, self.conf, self.cx, self.cx_conf = task, conf, cx, cx_conf
        self.needs_ws, self.verify, self.secrets = needs_ws, verify, secrets
        self.fail_privacy, self.fail_task, self.risk = fail_privacy, fail_task, risk
        self.business = business
        self.calls: list[tuple[str, str]] = []   # (statement, state) of every noul call
        self.form_calls = 0

    def form(self, state, questions):
        """The batch primitive, answered from the same knobs as the single questions (fail_task fails the whole form,
        as one real request would). The noul answers inside are recorded in `calls` like direct ones."""
        self.form_calls += 1
        if self.fail_task:
            raise RuntimeError("judge down")
        return form_via_primitives(self, state, questions)

    def choice(self, state, instructions, options):
        if self.fail_task:
            raise RuntimeError("judge down")
        return ChoiceAnswer(self.task, {k: (self.conf if k == self.task else 0) for k in options}, self.conf)

    def score(self, state, instructions, levels):
        if self.fail_task:
            raise RuntimeError("judge down")
        cx = self.risk if instructions == RISK_QUESTION else self.cx
        return ScoreAnswer(cx, [1.0 if i == cx else 0 for i in range(len(levels))], self.cx_conf)

    def noul(self, state, statement):
        self.calls.append((statement, state))
        if statement == CONTEXTUAL_PRIVACY_QUESTION:
            if self.fail_privacy:
                raise RuntimeError("judge down")
            return 1.0 if any(s in state for s in self.secrets) else 0.0
        if statement == NEEDS_WORKSPACE_QUESTION:
            return 1.0 if self.needs_ws else 0.0
        if statement in (STUCK_QUESTION, SMUGGLE_QUESTION):
            return 0.0
        if statement == BUSINESS_DATA_QUESTION:
            return 1.0 if self.business else 0.0
        return self.verify


class FakeEmbedder:
    """Bag-of-words vectors over a fixed vocabulary: deterministic, no network."""
    VOCAB = ["csv", "openpyxl", "spreadsheet", "swift", "xcode", "build", "test", "pytest", "sandbox", "network",
             "excel", "simulator", "python", "docker", "file"]

    def __init__(self):
        self.failed = False
        self.calls = 0

    def embed(self, text):
        self.calls += 1
        words = text.lower().replace(".", " ").replace(",", " ").split()
        v = [float(sum(w.startswith(t) for w in words)) for t in self.VOCAB]
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]


class FakeConnector:
    """A connector that returns a fixed business figure and saves it to the data room. Records its calls."""
    name, title = "fake", "Fake business data"
    FIGURE = "REVENUE 98765.43"

    def __init__(self, state="ready"):
        self.calls: list[dict] = []
        self._state = state

    def status(self):
        return Status(self._state, "" if self._state == "ready" else "not set up")

    def instructions(self):
        return "FAKE_CHEATSHEET: call fake_lookup(query) for business numbers."

    def tools(self):
        async def call(args, ctx):
            self.calls.append(dict(args))
            p = ctx.room_path("fake.json")
            p.write_text(json.dumps({"figure": self.FIGURE}))
            return ConnectorResult(preview=self.FIGURE, files=(p,), label="lookup")
        return [ConnectorTool("fake_lookup", "Look up the user's business numbers.",
                              {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
                              call)]


class Script:
    """A fake model that plays steps in order. Each step is (messages, info) -> ModelResponse; once exhausted it
    returns the default final result."""

    def __init__(self, steps: list[Callable] | None = None, final: Callable | None = None, name="fake"):
        self.steps = list(steps or [])
        self.final = final
        self.seen: list[list] = []   # the messages the model saw on each call
        self.infos: list[AgentInfo] = []   # the tools offered on each call
        self.model = FunctionModel(self._call, stream_function=self._stream, model_name=name)

    def _call(self, messages, info: AgentInfo) -> ModelResponse:
        self.seen.append(list(messages))
        self.infos.append(info)
        if self.steps:
            return self.steps.pop(0)(messages, info)
        if self.final:
            return self.final(messages, info)
        if info.output_tools:  # agents with structured output (the executor) default to a done report
            return final()(messages, info)
        return ModelResponse(parts=[TextPart("Done")])

    async def _stream(self, messages, info: AgentInfo):
        resp = self._call(messages, info)
        for i, part in enumerate(resp.parts):
            if isinstance(part, TextPart):
                yield part.content
            elif isinstance(part, ToolCallPart):
                yield {i: DeltaToolCall(name=part.tool_name, json_args=json.dumps(part.args, ensure_ascii=False),
                                        tool_call_id=part.tool_call_id)}

    def sent_text(self) -> str:
        """Everything non-model-generated the model ever received (used to assert that no private data went outbound)."""
        out = []
        for msgs in self.seen:
            for m in msgs:
                if isinstance(m, ModelRequest):
                    for p in m.parts:
                        if isinstance(p, UserPromptPart):
                            if isinstance(p.content, str):
                                out.append(p.content)
                            else:  # tool attachments (screenshot images) are not text; keep only the text parts
                                out.extend(c for c in p.content if isinstance(c, str))
                        elif isinstance(p, ToolReturnPart):
                            out.append(p.model_response_str())
                        elif isinstance(p, RetryPromptPart):
                            out.append(p.model_response())
        return "\n".join(out)


def tool(name: str, **args) -> Callable:
    return lambda m, info: ModelResponse(parts=[ToolCallPart(name, args)])


def text(t: str) -> Callable:
    return lambda m, info: ModelResponse(parts=[TextPart(t)])


def final(status="done", steps=("Task completed",), artifacts=(), issues=(), answer="LOCAL_ANSWER",
          verification=()) -> Callable:
    def f(m, info: AgentInfo):
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
            "report": {"status": status, "steps_done": list(steps), "artifacts": list(artifacts),
                       "issues": list(issues), "verification": list(verification)},
            "answer": answer})])
    return f


@pytest.fixture(scope="session")
def analyzer():
    return build_analyzer(Settings(custom_keywords=("ProjectCodenameA",)))


@pytest.fixture
def settings(tmp_path):
    return Settings(workspace=tmp_path / "ws", data_dir=tmp_path / "data", judge_samples=1,
                    cloud_api_key="test-key", mode=RunMode.AUTO, routellm_enabled=False,
                    stuck_check_every=0, env_path=tmp_path / ".env",
                    # the self-verification loop is off by default; the relevant tests enable it explicitly
                    verify_required=False, verify_rounds=0, cloud_exec=False, recon_enabled=False, lessons_enabled=False,
                    skills_enabled=False, connectors=[],
                    plan_design=False)  # the plan design phase is off by default; tests/test_plan_design.py enables it


@pytest.fixture
def make_agent(settings, analyzer):
    def _make(judge=None, executor=None, planner=None, cloud=None, compressor=None, reviewer=None, connectors=None, **overrides):
        for k, v in overrides.items():
            setattr(settings, k, v)
        models = ModelFactory(settings,
                              executor=(executor or Script(final=final())).model,
                              planner=planner.model if planner else None,
                              cloud=cloud.model if cloud else None,
                              compressor=compressor.model if compressor else None,
                              reviewer=reviewer.model if reviewer else None)
        if not settings.cloud_api_key:
            models._planner = models._cloud = None
        agent = Hermie(settings, judge=judge or FakeJudge(), analyzer=analyzer, scorer=False, models=models,
                       connectors=connectors)
        events = []
        agent.bus.subscribe(events.append)
        agent.events = events
        if agent.session.lessons is not None:  # never reach a real Ollama for embeddings
            agent.session.lessons.embedder = FakeEmbedder()
        if agent.session.skills is not None:
            agent.session.skills.embedder = FakeEmbedder()
        agent.learn_in_background = False  # deterministic tests: learning is awaited at the end of run()
        return agent
    return _make


def report_of(tool_return: str) -> dict:
    return json.loads(tool_return)


def review(passed: bool, problems=(), suggestions=()) -> Callable:
    """One step of the fake reviewer: outputs a structured Review."""
    def f(m, info: AgentInfo):
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
            "passed": passed, "problems": list(problems), "suggestions": list(suggestions)})])
    return f
