"""Business data stays on this machine: routing, session lock, and the planted figure never reaching the cloud."""
import json

from hermie.events import Notice, RouteDecided
from hermie.policy import BUSINESS_DATA_QUESTION

from .conftest import FakeConnector, FakeJudge, Script, final, review, text, tool

FIG = FakeConnector.FIGURE


async def test_prefix_routes_local_even_for_a_planning_task(make_agent):
    cloud, planner = Script(name="cloud"), Script(name="planner")
    agent = make_agent(FakeJudge(task="planning", needs_ws=False), cloud=cloud, planner=planner,
                       connectors=[FakeConnector()])
    r = await agent.run("Plan our pricing from last month's revenue", business=True)
    assert r.route == "local" and not cloud.seen and not planner.seen
    assert "business data: local only" in r.reasons


async def test_judge_vote_flags_business(make_agent):
    cloud = Script(name="cloud")
    agent = make_agent(FakeJudge(task="planning", needs_ws=False, business=True), cloud=cloud,
                       connectors=[FakeConnector()])
    r = await agent.run("Why did downloads drop last week?")
    assert r.route == "local" and agent.session.business and not cloud.seen
    ev = next(e for e in agent.events if isinstance(e, RouteDecided))
    assert ev.signals["business"] is True and ev.signals["business_prob"] == 1.0


async def test_business_question_not_asked_without_connectors(make_agent):
    judge = FakeJudge(task="repetitive", business=True)
    agent = make_agent(judge)
    r = await agent.run("Rename files")
    assert not agent.session.business
    assert all(stmt != BUSINESS_DATA_QUESTION for stmt, _ in judge.calls)


async def test_session_lock_survives_to_next_task(make_agent):
    cloud = Script([text("haiku")], name="cloud")
    agent = make_agent(FakeJudge(task="planning", needs_ws=False), cloud=cloud, connectors=[FakeConnector()])
    await agent.run("revenue?", business=True)
    r = await agent.run("Write a haiku about autumn")
    assert r.route == "local" and not cloud.seen
    assert any(isinstance(e, Notice) and "Business data (session)" in e.text for e in agent.events)


async def test_trajectory_has_business_flag_and_no_figure(make_agent, settings):
    ex = Script([tool("fake_lookup", query="q")], final=final(answer=f"It is {FIG}"))
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex, connectors=[FakeConnector()])
    await agent.run("revenue?")
    rec = json.loads(settings.trajectory_log_path.read_text().splitlines()[-1])
    assert rec["business"] is True
    assert FIG not in settings.trajectory_log_path.read_text()


async def test_per_question_path_asks_business_and_failure_counts_as_yes(make_agent):
    class Failing(FakeJudge):
        def noul(self, state, statement):
            if statement == BUSINESS_DATA_QUESTION:
                raise RuntimeError("judge down")
            return super().noul(state, statement)

    cloud = Script(name="cloud")
    agent = make_agent(Failing(task="planning", needs_ws=False), cloud=cloud, connectors=[FakeConnector()],
                       judge_batch=False)
    r = await agent.run("Summarize the quarter")
    assert r.route == "local" and agent.session.business and not cloud.seen
    ev = next(e for e in agent.events if isinstance(e, RouteDecided))
    assert ev.signals["business_prob"] == 1.0


from hermie.capabilities import OutboundBlockedError
from hermie.session import TaskState


async def test_outbound_guard_blocks_any_cloud_request_once_business(make_agent):
    from hermie.agents import build_cloud_agent
    cloud = Script([text("x")], name="cloud")
    agent = make_agent(cloud=cloud)
    st = TaskState(agent.session, "harmless text")
    st.business = True
    import pytest
    with pytest.raises(OutboundBlockedError):
        await build_cloud_agent(agent.models, planning=False).run("harmless text", deps=st)
    assert not cloud.seen


async def test_fallback_in_local_verify_does_not_escalate(make_agent, settings):
    cloud, planner = Script(name="cloud"), Script(name="planner")
    ex = Script([tool("fake_lookup", query="q")], final=final(answer=f"Revenue {FIG}"))
    agent = make_agent(FakeJudge(task="complex", cx=1, verify=0.1, needs_ws=False), executor=ex, cloud=cloud,
                       planner=planner, connectors=[FakeConnector()])
    r = await agent.run("how are we doing")
    assert r.route == "local_verify" and not cloud.seen and not planner.seen
    out_log = settings.outbound_log_path
    assert not out_log.exists() or FIG not in out_log.read_text()
    assert any("Business data: no self-check escalation" in n for n in r.reasons)


async def test_fallback_takes_sandbox_offline_mid_task(make_agent):
    ex = Script([tool("run_command", command="echo before"), tool("fake_lookup", query="q"),
                 tool("run_command", command="echo after")], final=final())
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex, connectors=[FakeConnector()])
    profiles = []
    orig = agent.session.sandbox.run_shell

    async def spy(*a, **kw):
        profiles.append(agent.session.sandbox.offline)
        return await orig(*a, **kw)
    agent.session.sandbox.run_shell = spy
    await agent.run("revenue?")
    assert profiles == [False, True]
    assert agent.session.sandbox.offline is False          # reset when the task ends


async def test_prefixed_task_is_offline_from_the_start(make_agent):
    ex = Script([tool("run_command", command="echo hi")], final=final())
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex, connectors=[FakeConnector()])
    seen = []
    orig = agent.session.sandbox.run_shell

    async def spy(*a, **kw):
        seen.append(agent.session.sandbox.offline)
        return await orig(*a, **kw)
    agent.session.sandbox.run_shell = spy
    await agent.run("revenue?", business=True)
    assert seen == [True]


async def test_failure_before_the_graph_still_takes_sandbox_back_online(make_agent):
    import pytest
    agent = make_agent(FakeJudge(task="repetitive"), connectors=[FakeConnector()])

    async def broken():
        raise RuntimeError("lesson store down")
    agent._sync_lessons = broken
    with pytest.raises(RuntimeError):
        await agent.run("revenue?", business=True)
    assert agent.session.sandbox.offline is False


async def test_business_lesson_not_in_agent_md_and_tagged(make_agent, settings):
    ex = Script([tool("fake_lookup", query="q")], final=final(answer=f"It is {FIG}"))
    rv = Script([review(False, problems=["Totals were estimated instead of computed"]), review(True)], name="reviewer")
    comp = Script([text("Compute totals from the saved file with python, never estimate.")], name="compressor")
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex, reviewer=rv, compressor=comp,
                       connectors=[FakeConnector()], lessons_enabled=True, verify_rounds=2)
    await agent.run("revenue?")
    doc = (settings.workspace / "AGENT.md").read_text()
    assert "Compute totals" not in doc and FIG not in doc
    assert "business-data question" in doc and "revenue?" not in doc
    lesson = next(l for l in agent.session.lessons.all() if "Compute totals" in l.text)
    assert lesson.business


PLAYBOOK = """# Look up and tabulate figures

## When to use
Answering a question from a figures lookup and listing files.

## Steps
1. Look up the figure, list the files, report.

## Verify
- The answer names the figure."""


async def _skill_run(make_agent, first_tool, **kw):
    ex = Script([first_tool] + [tool("list_files")] * 3, final=final(steps=["done"], verification=["listed"]))
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex, reviewer=Script([review(True)], name="reviewer"),
                       compressor=Script([text(PLAYBOOK)], name="compressor"), skills_enabled=True,
                       skill_min_tool_calls=1, verify_rounds=1, **kw)
    await agent.run("revenue?")
    return agent.session.skills.all()


async def test_business_task_distills_no_skill(make_agent):
    assert await _skill_run(make_agent, tool("fake_lookup", query="q"), connectors=[FakeConnector()]) == []
    # control (same data_dir, run after): the same setup without business data does produce a candidate,
    # so the empty result above is not just a disabled pipeline
    assert len(await _skill_run(make_agent, tool("list_files"))) == 1


async def test_new_session_clears_lock_history_and_app(make_agent):
    agent = make_agent(FakeJudge(task="repetitive"), connectors=[FakeConnector()])
    await agent.run("revenue?", business=True)
    agent.session.connector_session["asc"] = {"app": "1"}
    agent.new_session()
    assert not agent.session.business and agent.session.exec_history == [] and agent.session.connector_session == {}


async def test_list_and_set_default_app(make_agent, tmp_path):
    from pathlib import Path
    from hermie.connectors.asc import AscConnector
    from .test_connectors_asc import FakeRunner
    conn = AscConnector(Path("/opt/homebrew/bin/asc"), runner=FakeRunner(),
                        sync_runner=lambda a, e, t: (0, '{"credentials": [{"name": "x"}]}', ""))
    conn.binary = Path(__file__)   # exists, so status() is ready
    conn._status = None
    agent = make_agent(connectors=[conn])
    assert await agent.list_apps() == ["Alpha Notes", "Beta Fit"]
    assert await agent.set_default_app("beta") == "Beta Fit"
    assert agent.session.connector_session["asc"]["app"] == "222"
    import pytest
    with pytest.raises(LookupError):
        await agent.set_default_app("zzz")


async def test_new_session_refused_while_task_running(make_agent):
    agent = make_agent()
    agent.session.business = True
    agent.task_running = True
    assert agent.new_session() is False and agent.session.business
    agent.task_running = False
    assert agent.new_session() is True and not agent.session.business
