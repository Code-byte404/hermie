"""Business data stays on this machine: routing, session lock, and the planted figure never reaching the cloud."""
import json

from hermie.events import Notice, RouteDecided

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
    from hermie.policy import BUSINESS_DATA_QUESTION
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
            from hermie.policy import BUSINESS_DATA_QUESTION
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
