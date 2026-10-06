"""cloud_exec route: the cloud model drives the local sandbox tools; every tool result passes the privacy gate before it
goes out (Presidio findings are replaced by placeholders, placeholders in tool arguments are restored locally), and a
result the gate cannot certify hands the rest of the step to the local executor together with the history so far."""
from __future__ import annotations

from pydantic_ai.messages import ModelRequest, ToolReturnPart

from hermie.config import Settings
from hermie.events import Notice, OutboundSent
from hermie.judge import ChoiceAnswer, ScoreAnswer
from hermie.policy import Force, Route, Signals, decide

from .conftest import FakeJudge, Script, final, review, text, tool

PHONE = "13812345678"


def _sig(sensitive=False, needs_ws=True, business=False, task="complex", cx=2):
    return Signals(sensitive, ChoiceAnswer(task, {task: 1.0}, 1.0), ScoreAnswer(cx, [0, 0, 1.0], 1.0), 0.6,
                   needs_ws, 1.0 if needs_ws else 0.0, business=business)


# ---------------------------------------------------------------- policy

def test_cloud_exec_replaces_plan_for_clean_workspace_tasks():
    s = Settings(cloud_exec=True)
    d = decide(_sig(), s)
    assert d.route is Route.CLOUD_EXEC and any("tool result" in r for r in d.reasons)


def test_cloud_exec_is_opt_in():
    assert decide(_sig(), Settings(cloud_exec=False)).route is Route.PLAN


def test_sensitive_and_business_tasks_never_take_cloud_exec():
    s = Settings(cloud_exec=True)
    assert decide(_sig(sensitive=True), s).route is Route.PLAN
    assert decide(_sig(business=True), s).route is Route.LOCAL
    assert decide(_sig(), s, Force.LOCAL).route is Route.LOCAL
    assert decide(_sig(needs_ws=False), s).route is Route.CLOUD   # no workspace needed: the cloud answers directly


def test_route_label():
    assert Route.CLOUD_EXEC.label


# ---------------------------------------------------------------- the cloud model drives the local tools

def _tool_returns(script: Script) -> list[str]:
    return [str(p.content) for m in script.seen[-1] if isinstance(m, ModelRequest)
            for p in m.parts if isinstance(p, ToolReturnPart)]


async def test_cloud_model_drives_the_sandbox_tools(make_agent, settings):
    settings.workspace.mkdir(parents=True, exist_ok=True)
    cloud = Script([tool("write_file", path="fib.py", content="print(1)"), tool("run_command", command="python3 fib.py"),
                    final(steps=["Wrote fib.py", "Ran it"], verification=["python3 fib.py printed 1"])], name="cloud")
    ex = Script(name="executor")
    agent = make_agent(FakeJudge(task="complex", cx=2), executor=ex, cloud=cloud, cloud_exec=True)
    r = await agent.run("Write fib.py that prints 1 and run it")
    assert r.route == "cloud_exec" and r.backend == "deepseek-exec+ollama"
    assert (settings.workspace / "fib.py").read_text() == "print(1)"
    assert not ex.seen                                   # the local executor was never needed
    assert r.report["status"] == "done" and r.outbound_count >= 3
    returns = _tool_returns(cloud)
    assert any("exit_code=0" in t and "1" in t for t in returns)   # the command output went to the cloud model ...
    sent = [e.content for e in agent.events if isinstance(e, OutboundSent)]
    assert any("exit_code=0" in c for c in sent)                   # ... and was logged as outbound
    assert PHONE not in settings.outbound_log_path.read_text()


async def test_tool_results_with_pii_are_redacted_and_arguments_restored(make_agent, settings):
    settings.workspace.mkdir(parents=True, exist_ok=True)
    (settings.workspace / "clients.txt").write_text(f"call {PHONE} tomorrow\n")
    cloud = Script([tool("read_file", path="clients.txt"),
                    tool("edit_file", path="clients.txt", old="<CN_MOBILE_1>", new="<redacted>"),
                    tool("run_command", command="cat clients.txt"), final()], name="cloud")
    agent = make_agent(FakeJudge(task="complex", cx=2), cloud=cloud, cloud_exec=True)
    r = await agent.run("Redact the phone number in clients.txt")
    assert r.route == "cloud_exec"
    sent = cloud.sent_text()
    assert PHONE not in sent and "<CN_MOBILE_1>" in sent        # the cloud model saw the placeholder only
    assert (settings.workspace / "clients.txt").read_text() == "call <redacted> tomorrow\n"   # the real value was edited
    assert PHONE not in settings.outbound_log_path.read_text()


async def test_uncertifiable_result_hands_the_step_to_the_local_executor(make_agent, settings):
    """A contextual secret (not a pattern the rules can replace) blocks the cloud request; the local executor continues
    with the cloud run's tool history and finishes the task."""
    settings.workspace.mkdir(parents=True, exist_ok=True)
    (settings.workspace / "notes.txt").write_text("the merger with ProjectX closes Friday\n")
    cloud = Script([tool("read_file", path="notes.txt"), tool("write_file", path="never.txt", content="x")], name="cloud")
    ex = Script([tool("write_file", path="summary.txt", content="done locally")],
                final=final(steps=["Wrote the summary"], verification=["read it back"]))
    agent = make_agent(FakeJudge(task="complex", cx=2, secrets=("ProjectX",)), executor=ex, cloud=cloud, cloud_exec=True)
    r = await agent.run("Summarize notes.txt into summary.txt")
    assert r.route == "cloud_exec" and r.backend == "ollama"
    assert not (settings.workspace / "never.txt").exists()      # the cloud model never got its next turn
    assert (settings.workspace / "summary.txt").read_text() == "done locally"
    assert "ProjectX" not in cloud.sent_text() and "ProjectX" not in settings.outbound_log_path.read_text()
    assert "ProjectX" in ex.sent_text()                          # the local executor inherited the tool history
    assert any(isinstance(e, Notice) and "local executor continues" in e.text for e in agent.events)
    assert r.tainted


async def test_cloud_failure_hands_the_step_to_the_local_executor(make_agent, settings):
    def boom(m, info):
        raise RuntimeError("cloud down")
    cloud = Script([boom], name="cloud")
    ex = Script([tool("write_file", path="a.txt", content="x")], final=final(verification=["checked"]))
    agent = make_agent(FakeJudge(task="complex", cx=2), executor=ex, cloud=cloud, cloud_exec=True)
    r = await agent.run("Write a.txt")
    assert r.route == "cloud_exec" and r.backend == "ollama"
    assert (settings.workspace / "a.txt").read_text() == "x"


async def test_review_failure_sends_the_fix_round_to_the_cloud_executor(make_agent, settings):
    settings.workspace.mkdir(parents=True, exist_ok=True)
    cloud = Script([tool("write_file", path="out.csv", content="a,b\n"), tool("read_file", path="out.csv"),
                    final(steps=["Wrote the table"], verification=["read it back"]),
                    tool("edit_file", path="out.csv", old="a,b", new="a,b,c"), tool("read_file", path="out.csv"),
                    final(steps=["Added column c"], verification=["read it back"])], name="cloud")
    rev = Script([review(False, ["out.csv has only 2 columns"], ["Add column c"]), review(True)], name="reviewer")
    agent = make_agent(FakeJudge(task="complex", cx=2), cloud=cloud, reviewer=rev, cloud_exec=True, verify_rounds=2)
    r = await agent.run("Generate a 3-column CSV: a, b, c")
    assert (settings.workspace / "out.csv").read_text() == "a,b,c\n"
    assert "out.csv has only 2 columns" in cloud.sent_text()     # the local review's problems reached the cloud executor
    assert r.report["status"] == "done" and len(rev.seen) == 2


async def test_cloud_executor_has_its_own_tool_budget(make_agent, settings):
    """MAX_TOOL_CALLS protects against a slow local model looping; the cloud executor turns in seconds and finished
    the 2026-10-06 benchmark 'partial' only because it hit 30 calls. It gets CLOUD_MAX_TOOL_CALLS / CLOUD_MAX_REQUESTS."""
    settings.workspace.mkdir(parents=True, exist_ok=True)
    cloud = Script([tool("write_file", path=f"f{i}.txt", content="x") for i in range(4)] + [final(verification=["ls"])], name="cloud")
    agent = make_agent(FakeJudge(task="complex", cx=2), cloud=cloud, cloud_exec=True, max_tool_calls=1, max_requests=3,
                       cloud_max_tool_calls=10, cloud_max_requests=20)
    r = await agent.run("Create four files")
    assert all((settings.workspace / f"f{i}.txt").exists() for i in range(4))
    assert r.report["status"] == "done"
    assert not any("withdrawn" in str(p.content) for m in cloud.seen[-1] if isinstance(m, ModelRequest) for p in m.parts
                   if isinstance(p, ToolReturnPart))
