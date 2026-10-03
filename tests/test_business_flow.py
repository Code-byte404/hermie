"""Business data stays on this machine: routing, session lock, and the planted figure never reaching the cloud."""
import json

from hermie.events import Notice, RouteDecided
from hermie.policy import BUSINESS_DATA_QUESTION

from .conftest import FakeConnector, FakeJudge, Script, final, text, tool

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
