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
    assert nodes(st) == [("recall_lessons", "ok"), ("execute", "ok"), ("review", "again"), ("execute", "ok"), ("review", "done"),
                         ("finish_step", "ok")]
    assert st.trace[2]["passed"] is False and st.trace[2]["round"] == 1 and st.trace[4]["round"] == 2
    assert st.review_failures == 1 and st.review_fixed and st.step is None
    assert "Review failed (round 1)" in ex.sent_text() and "missing test" in ex.sent_text()


async def test_step_graph_no_review_when_rounds_zero(make_agent):
    rv = Script([review(False, problems=["should not be asked"])])
    agent = make_agent(FakeJudge(), executor=Script(final=final()), reviewer=rv, verify_rounds=0)
    st = TaskState(agent.session, "x")
    res = await run_step(agent, st, StepInput(prompt="x", task_text="x"))
    assert res.review is None and not rv.seen
    assert nodes(st) == [("recall_lessons", "ok"), ("execute", "ok"), ("review", "done"), ("finish_step", "ok")]


async def test_step_graph_keeps_last_review_when_reviewer_errors(make_agent):
    ex = Script([final(), final()])
    rv = Script([review(False, problems=["p1"]), lambda m, info: (_ for _ in ()).throw(RuntimeError("reviewer down"))])
    agent = make_agent(FakeJudge(), executor=ex, reviewer=rv, verify_rounds=3)
    st = TaskState(agent.session, "x")
    res = await run_step(agent, st, StepInput(prompt="x", task_text="x"))
    assert res.review is None  # as before the graph: a reviewer error ends the loop with no review
    assert len(ex.seen) == 2  # executed, fixed once, reviewer error ends the loop
    assert nodes(st)[-2:] == [("review", "done"), ("finish_step", "ok")]


async def test_step_graph_diagnoses_only_when_asked(make_agent):
    ex = Script([final(status="partial", issues=["could not compile"])])
    diag = Script([text("Compilation failed in one file")])
    agent = make_agent(FakeJudge(), executor=ex, compressor=diag, verify_rounds=1)
    st = TaskState(agent.session, "x")
    res = await run_step(agent, st, StepInput(prompt="x", task_text="x", diagnose=True))
    assert res.out.report.status is Status.PARTIAL and res.diagnosis == "Compilation failed in one file"
    assert nodes(st) == [("recall_lessons", "ok"), ("execute", "ok"), ("review", "diagnose"), ("diagnose", "ok"), ("finish_step", "ok")]
    st2 = TaskState(agent.session, "x")
    ex2 = Script([final(status="partial", issues=["could not compile"])])
    agent2 = make_agent(FakeJudge(), executor=ex2, compressor=Script([text("unused")]), verify_rounds=1)
    res2 = await run_step(agent2, st2, StepInput(prompt="x", task_text="x"))
    assert res2.diagnosis == "" and nodes(st2) == [("recall_lessons", "ok"), ("execute", "ok"), ("review", "done"), ("finish_step", "ok")]


# ---------------------------------------------------------------- task graph

PHONE = "13812345678"


def _rec(settings):
    import json
    return json.loads(settings.trajectory_log_path.read_text().splitlines()[-1])


def _raise(msg):
    def f(m, info):
        raise RuntimeError(msg)
    return f


async def test_local_route_nodes(make_agent, settings):
    agent = make_agent(FakeJudge(task="repetitive"), executor=Script(final=final()))
    r = await agent.run("Rename the files")
    assert r.route == "local" and r.snapshot_id
    rec = _rec(settings)
    assert nodes(rec) == [("route", "local"), ("snapshot", "execute"), ("recall_lessons", "ok"), ("execute", "ok"), ("review", "done"),
                          ("finish_step", "ok"), ("run_reviewed", "finish"), ("finish_local", "ok")]
    assert rec["nodes"][0]["reasons"] == r.reasons[:len(rec["nodes"][0]["reasons"])]


async def test_local_verify_passes_self_check(make_agent, settings):
    agent = make_agent(FakeJudge(task="simple", conf=0.4, cx=1, cx_conf=0.4, verify=0.9), executor=Script(final=final()))
    r = await agent.run("Explain this function")
    assert r.route == "local_verify" and r.reasons[-1].startswith("Local self-check passed")
    assert nodes(_rec(settings))[-3:] == [("run_reviewed", "self_check"), ("self_check", "passed"), ("finish_local", "ok")]


async def test_local_verify_escalates_to_cloud(make_agent, settings):
    cloud = Script([text("CLOUD_ANSWER")], name="cloud")
    agent = make_agent(FakeJudge(task="complex", conf=0.4, cx=1, cx_conf=0.4, verify=0.1, needs_ws=False),
                       executor=Script(final=final()), cloud=cloud)
    r = await agent.run("Compare the two designs")
    assert r.route == "local_verify" and r.backend == "deepseek" and r.output == "CLOUD_ANSWER"
    assert r.reasons[-1].startswith("Local self-check failed")
    rec = _rec(settings)
    assert nodes(rec)[-3:] == [("self_check", "escalate_cloud"), ("cloud_direct", "done"), ("finish_cloud", "ok")]
    assert rec["escalated"] is True and rec["fallback"] is False


async def test_local_verify_escalation_cloud_failure_falls_back(make_agent, settings):
    ex = Script([final(answer="first"), final(answer="second")])
    agent = make_agent(FakeJudge(task="complex", conf=0.4, cx=1, cx_conf=0.4, verify=0.1, needs_ws=False),
                       executor=ex, cloud=Script([_raise("api down")], name="cloud"))
    r = await agent.run("Compare the two designs")
    assert r.route == "local_verify" and r.backend == "ollama" and r.output == "second"
    own = [x for x in r.reasons if x.startswith("Local self-check failed") or "unavailable" in x]
    assert own[0].startswith("Local self-check failed") and "unavailable, falling back to local" in own[1]
    rec = _rec(settings)
    assert nodes(rec)[-8:] == [("cloud_direct", "fallback"), ("snapshot", "execute"), ("recall_lessons", "ok"), ("execute", "ok"), ("review", "done"),
                               ("finish_step", "ok"), ("run_reviewed", "finish"), ("finish_local", "ok")]
    assert rec["fallback"] is True and rec["escalated"] is True and len(ex.seen) == 2


async def test_cloud_route_nodes(make_agent, settings):
    agent = make_agent(FakeJudge(task="planning", needs_ws=False), cloud=Script([text("plan")], name="cloud"))
    r = await agent.run("Plan a launch")
    assert r.route == "cloud" and r.snapshot_id is None
    assert nodes(_rec(settings)) == [("route", "cloud"), ("cloud_direct", "done"), ("finish_cloud", "ok")]


async def test_cloud_outbound_block_falls_back_local(make_agent, settings, monkeypatch):
    cloud = Script([text("never")], name="cloud")
    agent = make_agent(FakeJudge(task="planning", needs_ws=False), cloud=cloud,
                       executor=Script(final=final(answer="local")))

    def refuse(text):
        raise PermissionError("Outbound check failed: test")
    monkeypatch.setattr(agent.session.gate, "certify", refuse)
    r = await agent.run("Plan a launch")
    assert r.route == "local" and not cloud.seen and r.output == "local"
    assert any("Outbound check blocked" in x for x in r.reasons)
    assert nodes(_rec(settings))[:2] == [("route", "cloud"), ("cloud_direct", "fallback")]


async def test_plan_route_nodes(make_agent, settings):
    planner = Script([tool("delegate", step="Create hello.py"), text("Done")], name="planner")
    agent = make_agent(FakeJudge(task="planning"), planner=planner, executor=Script(final=final()))
    r = await agent.run("Create a hello world script")
    assert r.route == "plan" and r.backend == "deepseek-plan+ollama"
    rec = _rec(settings)
    seq = nodes(rec)
    assert seq[:4] == [("route", "plan"), ("snapshot", "plan"), ("recon", "done"), ("outbound_task", "certified")]
    assert seq[-2:] == [("plan", "done"), ("finish_plan", "ok")]
    assert ("execute", "ok") in seq and rec["delegations"] == 1


async def test_plan_failure_midway_keeps_local_results(make_agent, settings):
    planner = Script([tool("delegate", step="Create hello.py"), _raise("planner down")], name="planner")
    ex = Script([final(answer="hello written")])
    agent = make_agent(FakeJudge(task="planning"), planner=planner, executor=ex)
    r = await agent.run("Create a hello world script")
    assert r.route == "plan" and "hello written" in r.output and len(ex.seen) == 1
    assert any("Planner failed midway" in x for x in r.reasons)
    assert nodes(_rec(settings))[-2:] == [("plan", "midway"), ("finish_plan", "ok")]


async def test_plan_without_cloud_key_runs_local(make_agent, settings):
    agent = make_agent(FakeJudge(task="planning"), cloud_api_key="", executor=Script(final=final()))
    r = await agent.run("Create a hello world script")
    assert r.route == "local" and "CLOUD_API_KEY" in "".join(r.reasons)
    assert nodes(_rec(settings))[:3] == [("route", "plan"), ("snapshot", "plan"), ("recon", "fallback")]


async def test_node_error_still_writes_trajectory(make_agent, settings):
    import pytest
    ex = Script([_raise("executor crashed")])
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex)
    with pytest.raises(RuntimeError):
        await agent.run("Rename the files")
    rec = _rec(settings)
    assert rec["interrupted"] is True and rec["route"] == "local"
    # the failing node and every node that was awaiting it are recorded as errors
    assert nodes(rec)[-2:] == [("execute", "error"), ("run_reviewed", "error")]
    assert rec["nodes"][-2]["error"] == "RuntimeError"
    assert (settings.workspace / "AGENT.md").exists()  # progress still recorded


async def test_task_graph_privacy_of_trajectory(make_agent, settings):
    planner = Script([tool("delegate", step="Summarize the list"), text("Summary done")], name="planner")
    ex = Script(final=final(answer=f"3 customers, first {PHONE}"))
    agent = make_agent(FakeJudge(task="planning"), executor=ex, planner=planner)
    await agent.run("Summarize the attached customer list", f"[File: /tmp/c.csv]\nname,phone\nZhang,{PHONE}\n")
    assert PHONE not in settings.trajectory_log_path.read_text()


def test_render_lists_every_node(make_agent):
    from hermie.graph import render
    src = render(make_agent(FakeJudge()))
    for node in ("route", "snapshot", "recall_lessons", "run_reviewed", "self_check", "cloud_direct", "recon", "outbound_task", "plan",
                 "finish_local", "finish_cloud", "finish_plan", "execute", "review", "diagnose", "finish_step"):
        assert f"\n  {node}\n" in src or f"  {node} -->" in src or f"--> {node}\n" in src, node
    assert src.count("stateDiagram-v2") == 2


async def test_local_verify_reviewer_error_does_not_escalate(make_agent):
    ex = Script([final(), final()])
    rv = Script([review(False, problems=["p1"]), _raise("reviewer down")])
    cloud = Script([text("never")], name="cloud")
    agent = make_agent(FakeJudge(task="simple", conf=0.4, cx=1, cx_conf=0.4, verify=0.9, needs_ws=False),
                       executor=ex, reviewer=rv, cloud=cloud, verify_rounds=3)
    r = await agent.run("Explain this function")
    assert r.route == "local_verify" and not cloud.seen and r.reasons[-1].startswith("Local self-check passed")


async def test_escalated_cloud_answer_has_no_local_report(make_agent):
    cloud = Script([text("CLOUD_ANSWER")], name="cloud")
    agent = make_agent(FakeJudge(task="complex", conf=0.4, cx=1, cx_conf=0.4, verify=0.1, needs_ws=False),
                       executor=Script(final=final(steps=["local step"])), cloud=cloud)
    r = await agent.run("Compare the two designs")
    assert r.backend == "deepseek" and r.report is None and r.artifacts == []


async def test_parallel_delegations_keep_their_own_results(make_agent):
    from pydantic_ai.messages import ModelResponse, ToolCallPart

    def two(m, info):
        return ModelResponse(parts=[ToolCallPart("delegate", {"step": "Step A"}),
                                    ToolCallPart("delegate", {"step": "Step B"})])
    planner = Script([two, text("Done")], name="planner")

    def by_step(m, info):
        seen = str(m[-1])
        failed = "Step B" in seen
        return final(status="partial" if failed else "done", issues=["b broke"] if failed else [],
                     steps=["b attempt" if failed else "a done"])(m, info)
    ex = Script(final=by_step)
    agent = make_agent(FakeJudge(task="planning"), planner=planner, executor=ex, compressor=Script([text("B failed")]),
                       verify_rounds=0)
    r = await agent.run("Do A and B")
    sent = planner.sent_text()
    assert "a done" in sent and "b broke" in sent
    returns = [p for msg in planner.seen[-1] for p in getattr(msg, "parts", []) if type(p).__name__ == "ToolReturnPart"]
    by_content = {("A" if "a done" in str(p.content) else "B"): str(p.content) for p in returns}
    assert "b broke" not in by_content["A"] and "failed" not in by_content["A"]
    assert "NoneType" not in sent
