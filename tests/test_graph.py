"""The task graph and the step graph: node sequences per route, recorded in TaskState.trace."""
from hermie.agents import Status
from hermie.graph import DECISIONS, StepInput, run_step, traced
from hermie.session import TaskState

from .conftest import FakeJudge, Script, final, review, text, tool


def nodes(st_or_rec):
    trace = st_or_rec.trace if isinstance(st_or_rec, TaskState) else st_or_rec["nodes"]
    return [(n["node"], n["status"]) for n in trace]


async def test_traced_records_duration_decision_and_notes(make_agent):
    agent = make_agent(FakeJudge())
    st = TaskState(agent.session, "x")

    class Ctx:
        state, deps = st, agent

    @traced("review")
    async def body(ctx):
        ctx.state.trace_note(passed=False)
        return "again"

    assert await body(Ctx) == "again"
    assert st.trace[0]["node"] == "review" and st.trace[0]["status"] == "again" and st.trace[0]["passed"] is False
    assert st.trace[0]["duration_s"] >= 0 and "again" in DECISIONS

    @traced("boom")
    async def failing(ctx):
        raise RuntimeError("no")

    try:
        await failing(Ctx)
    except RuntimeError:
        pass
    assert st.trace[1] == {"node": "boom", "duration_s": st.trace[1]["duration_s"], "status": "error", "error": "RuntimeError"}


async def test_step_graph_review_fails_then_passes(make_agent):
    ex = Script([final(steps=["wrote"]), final(steps=["fixed"])])
    rv = Script([review(False, problems=["missing test"]), review(True)])
    agent = make_agent(FakeJudge(), executor=ex, reviewer=rv, verify_rounds=2)
    st = TaskState(agent.session, "Write it")
    res = await run_step(agent, st, StepInput(prompt="Write it", task_text="Write it"))
    assert res.review is not None and res.review.passed and res.diagnosis == ""
    assert res.out.report.steps_done == ["fixed"]
    assert nodes(st) == [("execute", "ok"), ("review", "again"), ("execute", "ok"), ("review", "done"), ("finish_step", "ok")]
    assert st.trace[1]["passed"] is False and st.trace[1]["round"] == 1 and st.trace[3]["round"] == 2
    assert st.review_failures == 1 and st.review_fixed and st.step is None
    assert "Review failed (round 1)" in ex.sent_text() and "missing test" in ex.sent_text()


async def test_step_graph_no_review_when_rounds_zero(make_agent):
    rv = Script([review(False, problems=["should not be asked"])])
    agent = make_agent(FakeJudge(), executor=Script(final=final()), reviewer=rv, verify_rounds=0)
    st = TaskState(agent.session, "x")
    res = await run_step(agent, st, StepInput(prompt="x", task_text="x"))
    assert res.review is None and not rv.seen
    assert nodes(st) == [("execute", "ok"), ("review", "done"), ("finish_step", "ok")]


async def test_step_graph_keeps_last_review_when_reviewer_errors(make_agent):
    ex = Script([final(), final()])
    rv = Script([review(False, problems=["p1"]), lambda m, info: (_ for _ in ()).throw(RuntimeError("reviewer down"))])
    agent = make_agent(FakeJudge(), executor=ex, reviewer=rv, verify_rounds=3)
    st = TaskState(agent.session, "x")
    res = await run_step(agent, st, StepInput(prompt="x", task_text="x"))
    assert res.review is not None and not res.review.passed and res.review.problems == ["p1"]
    assert len(ex.seen) == 2  # executed, fixed once, reviewer error ends the loop
    assert nodes(st)[-2:] == [("review", "done"), ("finish_step", "ok")]


async def test_step_graph_diagnoses_only_when_asked(make_agent):
    ex = Script([final(status="partial", issues=["could not compile"])])
    diag = Script([text("Compilation failed in one file")])
    agent = make_agent(FakeJudge(), executor=ex, compressor=diag, verify_rounds=1)
    st = TaskState(agent.session, "x")
    res = await run_step(agent, st, StepInput(prompt="x", task_text="x", diagnose=True))
    assert res.out.report.status is Status.PARTIAL and res.diagnosis == "Compilation failed in one file"
    assert nodes(st) == [("execute", "ok"), ("review", "diagnose"), ("diagnose", "ok"), ("finish_step", "ok")]
    st2 = TaskState(agent.session, "x")
    ex2 = Script([final(status="partial", issues=["could not compile"])])
    agent2 = make_agent(FakeJudge(), executor=ex2, compressor=Script([text("unused")]), verify_rounds=1)
    res2 = await run_step(agent2, st2, StepInput(prompt="x", task_text="x"))
    assert res2.diagnosis == "" and nodes(st2) == [("execute", "ok"), ("review", "done"), ("finish_step", "ok")]
