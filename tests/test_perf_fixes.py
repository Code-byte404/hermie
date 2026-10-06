"""Fixes from the 2026-10-06 performance investigation: withdrawn tools answer instead of trapping the model in
"Unknown tool" retries, background judge checks (taint / stuck / command risk) use one sample, a local model's 500 is
retried once, and whole-file rewrites of the same file are capped."""
from __future__ import annotations

import json

import httpx
import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelRequest, RetryPromptPart, ToolReturnPart

from hermie.agents import (EXECUTOR_INSTRUCTIONS, REVIEWER_INSTRUCTIONS, REWRITE_LIMIT_NOTE, ArtifactInfo, ExecutorReport,
                           ModelFactory, Status, build_executor, build_reviewer)
from hermie.capabilities import RISK_QUESTION, STUCK_QUESTION, WITHDRAWN_NOTE
from hermie.config import Settings
from hermie.events import CommandFinished
from hermie.judge import OllamaJudge
from hermie.privacy import CONTEXTUAL_PRIVACY_QUESTION

from .conftest import FakeJudge, Script, final, review, text, tool


def _tool_returns(script: Script) -> list[str]:
    """Every tool result in the conversation as the model last saw it."""
    out = []
    for m in script.seen[-1]:
        if isinstance(m, ModelRequest):
            out += [str(p.content) for p in m.parts if isinstance(p, ToolReturnPart)]
    return out


# ---------------------------------------------------------------- 1. tool withdrawal is a message, not a retry trap

async def test_withdrawn_tools_answer_instead_of_failing_the_run(make_agent):
    """With the tool budget spent, further tool calls get a note asking for the report; before, they raised
    "Unknown tool name" until the retries ran out and the whole step failed with UnexpectedModelBehavior."""
    ex = Script([tool("run_command", command="echo 1"), tool("run_command", command="echo 2"),
                 tool("run_command", command="echo 3"), final(status="partial")])
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex, max_tool_calls=1, report_retries=0)
    r = await agent.run("Echo things")
    assert r.report and r.report["status"] == "partial"
    returns = _tool_returns(ex)
    assert sum(WITHDRAWN_NOTE in t for t in returns) == 2
    assert not any("Unknown tool" in t for t in returns)
    assert not any(e.__class__.__name__ == "Notice" and "Executor run failed" in getattr(e, "text", "") for e in agent.events)
    # refused calls are visible in the command log / UI as "withdrawn" (never the command text)
    refused = [e for e in agent.events if isinstance(e, CommandFinished) and e.summary == "withdrawn"]
    assert len(refused) == 2 and all(e.tool == "run_command" and e.exit_code == 1 for e in refused)


class StuckJudge(FakeJudge):
    def noul(self, state, statement):
        if statement == STUCK_QUESTION:
            self.calls.append((statement, state))
            return 1.0
        return super().noul(state, statement)


async def test_stuck_verdict_withdraws_tools_without_retry_trap(make_agent):
    ex = Script([tool("run_command", command="echo 1"), tool("run_command", command="echo 2"),
                 tool("run_command", command="echo 3"), final(status="partial")])
    agent = make_agent(StuckJudge(task="repetitive"), executor=ex, stuck_check_every=1, report_retries=0)
    r = await agent.run("Echo things")
    assert r.report and r.report["status"] == "partial"
    returns = _tool_returns(ex)
    assert any(WITHDRAWN_NOTE in t for t in returns)
    assert not any("Unknown tool" in t for t in returns)


# ---------------------------------------------------------------- 2. background judge checks use one sample

def _ollama_judge(samples: int, background: int, requests: list) -> OllamaJudge:
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        requests.append(body)
        allowed = body["format"]["properties"]["answer"]["enum"]   # "no" for yes/no, L0 for a score
        return httpx.Response(200, json={"message": {"content": json.dumps({"answer": allowed[-1]})},
                                         "prompt_eval_count": 10, "eval_count": 5})
    s = Settings(judge_samples=samples, background_judge_samples=background, ollama_url="http://ollama.test")
    return OllamaJudge(s, client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_quick_judge_samples_once_and_shares_the_client():
    reqs: list = []
    judge = _ollama_judge(samples=3, background=1, requests=reqs)
    judge.noul("tool output", CONTEXTUAL_PRIVACY_QUESTION)
    assert len(reqs) == 3
    quick = judge.with_samples(1)
    quick.noul("tool output", CONTEXTUAL_PRIVACY_QUESTION)
    assert len(reqs) == 4 and reqs[-1]["options"]["temperature"] == 0.0
    assert quick.score("rm -rf build", "risk?", ["low", "medium", "high"]).score == 2   # one request, the mock's last option
    assert len(reqs) == 5
    judge.noul("again", CONTEXTUAL_PRIVACY_QUESTION)   # the original judge still samples three times
    assert len(reqs) == 8


class RecordingFake(FakeJudge):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.score_calls: list[str] = []
        self.samples = None

    def score(self, state, instructions, levels):
        self.score_calls.append(instructions)
        return super().score(state, instructions, levels)


class RecordingJudge(FakeJudge):
    """A judge whose with_samples() hands out a separate recording judge, like OllamaJudge does."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.quick = RecordingFake(**kw)

    def with_samples(self, n: int):
        self.quick.samples = n
        return self.quick


async def test_background_checks_go_through_the_quick_judge(make_agent, settings):
    settings.workspace.mkdir(parents=True, exist_ok=True)
    (settings.workspace / "notes.txt").write_text("meeting notes, nothing private\n")
    ex = Script([tool("read_file", path="notes.txt"), tool("run_command", command="python3 -c 'print(1)'")])
    judge = RecordingJudge(task="repetitive")
    agent = make_agent(judge, executor=ex, stuck_check_every=2, background_judge_samples=1)
    await agent.run("Read notes.txt")
    quick_qs = [q for q, _ in judge.quick.calls]
    assert judge.quick.samples == 1
    assert CONTEXTUAL_PRIVACY_QUESTION in quick_qs      # taint check on the file content
    assert STUCK_QUESTION in quick_qs                   # stuck check after two tool calls
    assert RISK_QUESTION in judge.quick.score_calls     # inline python -> command risk judge
    main_qs = [q for q, _ in judge.calls]
    assert STUCK_QUESTION not in main_qs
    # the routing-time privacy check still uses the full judge
    assert CONTEXTUAL_PRIVACY_QUESTION in main_qs


# ---------------------------------------------------------------- 3. one retry on a local model's 500

def _server_error(m, info):
    raise ModelHTTPError(status_code=500, model_name="qwen", body={"message": "XML syntax error on line 11"})


async def test_executor_request_is_retried_once_after_a_500(make_agent, settings):
    settings.workspace.mkdir(parents=True, exist_ok=True)
    ex = Script([_server_error, tool("write_file", path="a.txt", content="hello"), final()])
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex)
    r = await agent.run("Write a.txt")
    assert r.report and r.report["status"] == "done"
    assert (settings.workspace / "a.txt").read_text() == "hello"


async def test_reviewer_request_is_retried_once_after_a_500(make_agent, settings):
    settings.workspace.mkdir(parents=True, exist_ok=True)
    ex = Script([tool("write_file", path="a.txt", content="hello"), tool("run_command", command="cat a.txt"), final()])
    rv = Script([_server_error, review(True)])
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex, reviewer=rv, verify_rounds=1)
    await agent.run("Write a.txt")
    assert len(rv.seen) == 2
    assert not any("Reviewer error" in getattr(e, "text", "") for e in agent.events)


async def test_second_500_in_a_row_still_fails(make_agent, settings):
    """Exactly one retry: a second 5xx propagates like any other local model error."""
    ex = Script([_server_error, _server_error, final()])
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex)
    with pytest.raises(ModelHTTPError):
        await agent.run("Do something")
    assert len(ex.seen) == 2


# ---------------------------------------------------------------- 4. whole-file rewrites are capped

async def test_fourth_rewrite_of_the_same_file_is_refused(make_agent, settings):
    settings.workspace.mkdir(parents=True, exist_ok=True)
    (settings.workspace / "t.py").write_text("v0")
    ex = Script([tool("write_file", path="t.py", content=f"v{i}") for i in range(1, 5)] + [final()])
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex, write_rewrite_limit=3)
    await agent.run("Rewrite t.py")
    assert (settings.workspace / "t.py").read_text() == "v3"
    returns = _tool_returns(ex)
    assert sum(REWRITE_LIMIT_NOTE in t for t in returns) == 1
    assert "edit_file" in REWRITE_LIMIT_NOTE


async def test_new_files_and_other_paths_do_not_count_as_rewrites(make_agent, settings):
    settings.workspace.mkdir(parents=True, exist_ok=True)
    ex = Script([tool("write_file", path=f"f{i}.txt", content="x") for i in range(5)] + [final()])
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex, write_rewrite_limit=1)
    await agent.run("Create files")
    assert all((settings.workspace / f"f{i}.txt").exists() for i in range(5))
    assert not any(REWRITE_LIMIT_NOTE in t for t in _tool_returns(ex))


# ---------------------------------------------------------------- 5. the structured result is an explicit, named tool call

async def test_report_and_review_are_named_output_tools(make_agent, settings):
    """gemma4 wrote its final report as prose (echoing the instruction text) and never called the anonymous output tool;
    pydantic-ai then parsed the prose as JSON and the model argued with the JSON error for four turns. The output tools
    now have names the instructions can point at."""
    settings.workspace.mkdir(parents=True, exist_ok=True)
    ex = Script([tool("write_file", path="a.txt", content="x"), tool("run_command", command="cat a.txt"), final()])
    rv = Script([review(True)])
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex, reviewer=rv, verify_rounds=1)
    await agent.run("Write a.txt")
    assert [t.name for t in ex.infos[0].output_tools] == ["submit_report"]
    assert [t.name for t in rv.infos[0].output_tools] == ["submit_review"]
    assert "submit_report" in EXECUTOR_INSTRUCTIONS and "submit_review" in REVIEWER_INSTRUCTIONS


async def test_prose_instead_of_the_report_tool_is_sent_back_with_the_tool_name(make_agent, settings):
    ex = Script([text("Done. report: status done; steps_done: wrote the file."), final()])
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex)
    r = await agent.run("Say done")
    assert r.report and r.report["status"] == "done"
    retries = [str(p.content) for m in ex.seen[-1] if isinstance(m, ModelRequest) for p in m.parts if isinstance(p, RetryPromptPart)]
    assert len(retries) == 1 and "submit_report" in retries[0] and "Invalid JSON" not in retries[0]


# ---------------------------------------------------------------- 6. tolerant report schema for small local models

def test_report_without_status_infers_it_from_verification_and_issues():
    """gemma4 kept omitting report.status and never repaired it from the validation error: a verified report without
    issues counts as done, anything else as partial (never as done without verification)."""
    assert ExecutorReport.model_validate({"verification": ["ran pytest: 3 passed"]}).status is Status.DONE
    assert ExecutorReport.model_validate({}).status is Status.PARTIAL
    assert ExecutorReport.model_validate({"verification": ["ran it"], "issues": ["tests fail"]}).status is Status.PARTIAL
    assert ExecutorReport.model_validate({"status": "failed", "verification": ["x"]}).status is Status.FAILED


def test_artifact_accepts_a_numeric_size_instead_of_size_hint():
    assert ArtifactInfo.model_validate({"path": "a.py", "type": "file", "size": 913}).size_hint == "913 bytes"
    assert ArtifactInfo.model_validate({"path": "a.py", "type": "file"}).size_hint == ""
    assert ArtifactInfo.model_validate({"path": "a.py", "type": "file", "size_hint": "14 lines"}).size_hint == "14 lines"


def test_executor_has_extra_output_retries_for_empty_or_malformed_results(settings):
    settings.report_retries, settings.extra_output_retries = 3, 4
    models = ModelFactory(settings, executor=Script().model, reviewer=Script().model)
    assert build_executor(models, [])._max_output_retries == 3 + 1 + 4
    assert build_reviewer(models)._max_output_retries == 2 + 4   # the reviewer's verdict on a long diff fails the same way
