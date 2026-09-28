"""The task graph and the step graph on pydantic-graph.

Nodes are async functions on a GraphBuilder; TaskState is the graph state, the Hermie instance the deps. Node
bodies call the same Hermie helpers the old handlers used (_run_executor, _review, _diagnose, _snapshot,
_outbound_task, _local_output), so the privacy path is unchanged: the outbound gate, OutboundGuard, validate_report
and restore_local all live inside node bodies, never on an edge that could be skipped.

Every node is wrapped by `traced()`: one record per node in TaskState.trace (duration, decision, and the structured
notes the body left with TaskState.trace_note), written to trajectories.jsonl by trajectory.task_record at task end.

The step graph runs one executor task with the local review loop:

    [*] --> execute --> review --> (again) execute | (done) finish_step | (diagnose) diagnose --> finish_step --> [*]

Both the local routes (run_reviewed in the task graph) and the planner's delegate tool run it through run_step().
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from functools import wraps
from typing import TYPE_CHECKING, Literal, Optional

from pydantic_graph import GraphBuilder, StepContext, TypeExpression

from .agents import (ExecutorOutput, Review, Status, build_cloud_agent, build_planner, require_clean, restore_local,
                     stream_handler, usage_limits)
from .audit import sha256
from .events import ChatMessage, Notice, ReviewArrived, RouteDecided
from .policy import Route
from .recon import workspace_recon
from .session import TaskState

if TYPE_CHECKING:
    from .core import Hermie, TaskResult

log = logging.getLogger(__name__)

# Every decision value a node can return. traced() records these, and only these, as the node status; anything else
# a node returns is recorded as "ok" so that no text ever lands in the trajectory.
DECISIONS = frozenset({"again", "done", "diagnose",                       # step graph
                       "local", "local_verify", "cloud", "plan",           # route
                       "execute", "finish", "self_check", "passed", "escalate_plan", "escalate_cloud",
                       "certified", "fallback", "midway"})


def traced(node: str):
    """Wrap a node body: record duration, decision and the body's trace notes; re-raise whatever it raises."""
    def deco(fn):
        @wraps(fn)
        async def wrapper(ctx):
            st: TaskState = ctx.state
            t0 = time.monotonic()
            try:
                result = await fn(ctx)
            except BaseException as e:
                st.trace_add(node, duration_s=round(time.monotonic() - t0, 3), status="error", error=type(e).__name__)
                raise
            status = result if isinstance(result, str) and result in DECISIONS else "ok"
            st.trace_add(node, duration_s=round(time.monotonic() - t0, 3), status=status)
            return result
        return wrapper
    return deco


# ---------------------------------------------------------------- step graph

@dataclass
class StepInput:
    prompt: str                                   # what the executor is asked to do (local text)
    task_text: str                                # what the reviewer judges against: the task, or the delegated step
    acceptance: list[str] = field(default_factory=list)
    diagnose: bool = False                        # plan mode: on failure, a data-free diagnosis for the planner


@dataclass
class StepResult:
    out: ExecutorOutput
    review: Optional[Review] = None
    diagnosis: str = ""


@dataclass
class _StepRun:
    """Working memory of one step-graph run, kept on TaskState.step."""
    inp: StepInput
    prompt: str                                   # the current prompt: the original, then the fix prompt
    out: Optional[ExecutorOutput] = None
    review: Optional[Review] = None               # last review the reviewer actually produced
    round: int = 0
    diagnosis: str = ""


StepCtx = StepContext[TaskState, object, object]


@traced("execute")
async def _execute(ctx: StepCtx) -> ExecutorOutput:
    st, agent = ctx.state, ctx.deps
    run: _StepRun = st.step
    run.out = await agent._run_executor(st, run.prompt)
    st.trace_note(report_status=run.out.report.status.value, issues=len(run.out.report.issues),
                  tool_calls=st.tool_calls, stuck=st.stuck)
    return run.out


@traced("review")
async def _review(ctx: StepCtx) -> Literal["again", "done", "diagnose"]:
    """After the executor claims done, the local reviewer verifies; on failure the problems go back to the executor
    to fix, up to verify_rounds rounds; a reviewer error ends the loop with no review. One iteration per visit."""
    st, agent = ctx.state, ctx.deps
    run: _StepRun = st.step
    out = run.out
    rounds = agent.s.verify_rounds
    decision: Literal["again", "done", "diagnose"] = "done"
    if out.report.status is Status.DONE and run.round < rounds:
        run.round += 1
        rnd = run.round
        review = await agent._review(st, run.inp.task_text, run.inp.acceptance, out)
        run.review = review  # a reviewer error ends the loop with no review, as the old loop did
        if review is not None:
            last = review.passed or rnd == rounds
            st.last_review = review.model_dump()
            st.review_history.append(st.last_review)
            st.bus.emit(ReviewArrived(review.passed, review.problems, review.suggestions, rnd, last))
            if st.session.review_log:
                st.session.review_log.write({"task": sha256(st.text)[:12], "route": st.route, "round": rnd,
                                             "passed": review.passed, "problems": review.problems,
                                             "suggestions": review.suggestions})
            if review.passed:
                if st.review_failures:
                    st.review_fixed = True
            else:
                st.review_failures += 1
                if not last:
                    run.prompt = (f"{run.inp.prompt}\n\n[Review failed (round {rnd})]\nProblems:\n"
                                  + "\n".join(f"- {x}" for x in review.problems) + "\nSuggestions:\n"
                                  + "\n".join(f"- {x}" for x in review.suggestions)
                                  + "\nFix each item, verify, then report.")
                    decision = "again"
            st.trace_note(round=rnd, passed=review.passed, problems=len(review.problems))
    if decision == "done" and run.inp.diagnose:
        failed = out.report.status is not Status.DONE or (run.review is not None and not run.review.passed)
        if failed:
            decision = "diagnose"
    return decision


@traced("diagnose")
async def _diagnose(ctx: StepCtx) -> None:
    st, agent = ctx.state, ctx.deps
    run: _StepRun = st.step
    run.diagnosis = await agent._diagnose(st, run.out, run.review)
    st.trace_note(diagnosis=bool(run.diagnosis))


@traced("finish_step")
async def _finish_step(ctx: StepCtx) -> StepResult:
    run: _StepRun = ctx.state.step
    return StepResult(run.out, run.review, run.diagnosis)


def build_step_graph(agent: "Hermie"):
    g = GraphBuilder(name="step", state_type=TaskState, deps_type=object, output_type=StepResult)
    execute = g.step(_execute, node_id="execute")
    review = g.step(_review, node_id="review")
    diagnose = g.step(_diagnose, node_id="diagnose")
    finish = g.step(_finish_step, node_id="finish_step")
    g.add(
        g.edge_from(g.start_node).to(execute),
        g.edge_from(execute).to(review),
        g.edge_from(review).to(
            g.decision(node_id="review_outcome")
            .branch(g.match(TypeExpression[Literal["again"]]).label("failed, rounds left").to(execute))
            .branch(g.match(TypeExpression[Literal["diagnose"]]).label("failed, plan mode").to(diagnose))
            .branch(g.match(TypeExpression[Literal["done"]]).to(finish))),
        g.edge_from(diagnose).to(finish),
        g.edge_from(finish).to(g.end_node),
    )
    return g.build()


async def run_step(agent: "Hermie", st: TaskState, inp: StepInput) -> StepResult:
    """Run the step graph once for this task state (the caller has taken the snapshot the reviewer diffs against)."""
    st.step = _StepRun(inp=inp, prompt=inp.prompt)
    try:
        return await agent.step_graph.run(state=st, deps=agent)
    finally:
        st.step = None


# ---------------------------------------------------------------- task graph

VERIFY_QUESTION = ("Does the answer below complete the task correctly and completely, so that a stronger model does not "
                   "need to redo it?")

TaskCtx = StepContext[TaskState, object, object]


def _fallback(st: TaskState, why: str, notify: bool = True) -> Literal["fallback"]:
    """A cloud step could not proceed: finish the task locally (the old core._fallback_local / _local(reasons))."""
    if notify:
        st.bus.emit(Notice("warn", why))
    st.flow.notes.append(why)
    st.flow.fallback = True
    return "fallback"


@traced("route")
async def _route(ctx: TaskCtx) -> Literal["local", "local_verify", "cloud", "plan"]:
    st, agent = ctx.state, ctx.deps
    routing = await agent.router.route(st.flow.task, st.text, st.flow.force)
    st.flow.routing = routing
    d = routing.decision
    st.sensitive_input = routing.verdict.sensitive
    st.route = d.route.value
    st.bus.emit(RouteDecided(d.route.value, d.reasons, routing.signals_dict()))
    st.trace_note(sensitive=routing.verdict.sensitive, reasons=list(d.reasons))
    return d.route.value


@traced("snapshot")
async def _snapshot(ctx: TaskCtx) -> Literal["execute", "plan"]:
    """Ensure the pre-task snapshot exists (the reviewer diffs against it; rollback returns to it)."""
    st, agent = ctx.state, ctx.deps
    if st.snapshot_id is None:
        st.snapshot_id = agent._snapshot(st)
    st.flow.task_snapshot_id = st.flow.task_snapshot_id or st.snapshot_id
    if st.flow.fallback or st.route != Route.PLAN.value:
        return "execute"
    return "plan"


@traced("run_reviewed")
async def _run_reviewed(ctx: TaskCtx) -> Literal["finish", "self_check"]:
    st, agent = ctx.state, ctx.deps
    st.flow.last_step = await run_step(agent, st, StepInput(prompt=st.text, task_text=st.text))
    if st.route == Route.LOCAL_VERIFY.value and not st.flow.fallback:
        return "self_check"
    return "finish"


@traced("self_check")
async def _self_check(ctx: TaskCtx) -> Literal["passed", "escalate_plan", "escalate_cloud"]:
    """local_verify: after the local run, the judge decides whether a stronger model needs to redo it."""
    st, agent = ctx.state, ctx.deps
    res: StepResult = st.flow.last_step
    out, review = res.out, res.review
    if review is not None and not review.passed:
        note = "Local review failed after multiple rounds; escalating to cloud"
        p = 0.0
    else:
        state = (f"Task:\n{st.text}\n\nExecutor report:\n{out.report.model_dump_json()}\n\nAnswer:\n{out.answer}"
                 + (f"\n\nLocal review: {json.dumps(st.last_review, ensure_ascii=False)}" if st.last_review else ""))
        try:
            p = await asyncio.to_thread(st.session.judge.noul, state, VERIFY_QUESTION)
        except Exception as e:
            st.flow.notes.append(f"Self-check failed; keeping the local result: {e}")
            st.trace_note(judge_error=type(e).__name__)
            return "passed"
        st.trace_note(p=round(p, 3))
        if p >= agent.s.verify_threshold and out.report.status is Status.DONE:
            st.flow.notes.append(f"Local self-check passed p={p:.2f}")
            return "passed"
        note = f"Local self-check failed p={p:.2f}; escalating to cloud"
    st.bus.emit(Notice("info", note))
    st.flow.notes.append(note)
    st.flow.escalated = True
    routing = st.flow.routing
    needs_ws = routing.signals.needs_workspace if routing.signals else True
    if needs_ws or st.tainted or st.artifacts:
        return "escalate_plan"
    st.answers.clear()
    return "escalate_cloud"


@traced("cloud_direct")
async def _cloud_direct(ctx: TaskCtx) -> Literal["done", "fallback"]:
    st, agent = ctx.state, ctx.deps
    routing = st.flow.routing
    planning = bool(routing.signals and routing.signals.task.choice == "planning")
    try:
        clean = st.remember(await asyncio.to_thread(st.gate.certify, st.text))  # check once more before going outbound
    except PermissionError as e:
        return _fallback(st, f"Outbound check blocked; running locally instead: {e}")
    try:
        cloud_agent = build_cloud_agent(agent.models, planning)
        res = await cloud_agent.run(require_clean(clean), deps=st, event_stream_handler=stream_handler("planner", st.bus))
    except Exception as e:  # includes OutboundBlockedError, network errors, missing API key
        log.exception("Cloud model call failed")
        return _fallback(st, f"{agent.s.cloud_label} unavailable, falling back to local: {e}")
    st.bus.emit(ChatMessage("planner", res.output))
    st.flow.cloud_output = res.output
    return "done"


@traced("recon")
async def _recon(ctx: TaskCtx) -> Literal["done", "fallback"]:
    st, agent = ctx.state, ctx.deps
    if not agent.models.cloud_available:
        return _fallback(st, "CLOUD_API_KEY not set; plan mode runs fully local")
    if agent.s.recon_enabled:
        try:
            st.flow.recon = await workspace_recon(agent.session.sandbox, bool(st.project_doc))
        except Exception as e:
            log.exception("Recon failed")
            st.flow.notes.append(f"Workspace recon failed; the planner starts blind: {type(e).__name__}")
    st.trace_note(recon=bool(st.flow.recon))
    return "done"


@traced("outbound_task")
async def _outbound_task(ctx: TaskCtx) -> Literal["certified", "fallback"]:
    st, agent = ctx.state, ctx.deps
    outbound = await agent._outbound_task(st, st.flow.routing.verdict, st.flow.notes, st.flow.recon)
    if outbound is None:
        return _fallback(st, "de-identification failed; running fully local", notify=False)
    st.flow.outbound = outbound
    return "certified"


@traced("plan")
async def _plan(ctx: TaskCtx) -> Literal["done", "midway", "fallback"]:
    st, agent = ctx.state, ctx.deps
    st.report_for_cloud = True
    try:
        planner = build_planner(agent.models, agent._delegated_step)
        res = await planner.run(require_clean(st.flow.outbound), deps=st, usage_limits=usage_limits(agent.s),
                                event_stream_handler=stream_handler("planner", st.bus))
    except Exception as e:
        log.exception("Plan mode failed")
        st.report_for_cloud = False
        st.trace_note(delegations=st.delegations, error=type(e).__name__)
        if st.answers:  # the executor already did part of the work: keep the results, do not rerun
            st.flow.notes.append(f"Planner failed midway ({e}); keeping the finished local results")
            return "midway"
        return _fallback(st, f"Planner unavailable; running fully local: {e}", notify=False)
    st.flow.plan_summary = restore_local(st, res.output)
    st.bus.emit(ChatMessage("planner", st.flow.plan_summary))
    st.trace_note(delegations=st.delegations)
    return "done"


def _result(st: TaskState, output: str, route: Route, backend: str, snapshot_id: Optional[str],
            local_work: bool = True) -> "TaskResult":
    """local_work=False for a cloud answer: the report and artifacts of a rejected local run do not describe it."""
    from .core import TaskResult
    route_value = Route.LOCAL_VERIFY.value if st.route == Route.LOCAL_VERIFY.value else route.value
    return TaskResult(output, route_value, backend, list(st.flow.notes), snapshot_id=snapshot_id,
                      report=st.last_report if local_work else None, artifacts=st.artifacts if local_work else [])


@traced("finish_local")
async def _finish_local(ctx: TaskCtx) -> "TaskResult":
    st, agent = ctx.state, ctx.deps
    return _result(st, agent._local_output(st), Route.LOCAL, "ollama", st.flow.task_snapshot_id)


@traced("finish_cloud")
async def _finish_cloud(ctx: TaskCtx) -> "TaskResult":
    st, agent = ctx.state, ctx.deps
    return _result(st, st.flow.cloud_output, Route.CLOUD, agent.s.cloud_provider, st.flow.task_snapshot_id,
                   local_work=False)


@traced("finish_plan")
async def _finish_plan(ctx: TaskCtx) -> "TaskResult":
    st, agent = ctx.state, ctx.deps
    backend = f"{agent.s.cloud_provider}-plan+ollama"
    if st.flow.plan_summary is None:  # planner failed midway
        return _result(st, agent._local_output(st), Route.PLAN, backend, st.flow.task_snapshot_id)
    output = st.flow.plan_summary + ("\n\n---\nLocal execution result:\n" + agent._local_output(st)
                                     if st.answers or st.artifacts else "")
    return _result(st, output, Route.PLAN, backend, st.flow.task_snapshot_id)


def build_task_graph(agent: "Hermie"):
    from .core import TaskResult
    g = GraphBuilder(name="task", state_type=TaskState, deps_type=object, output_type=TaskResult)
    route = g.step(_route, node_id="route")
    snapshot = g.step(_snapshot, node_id="snapshot")
    run_reviewed = g.step(_run_reviewed, node_id="run_reviewed")
    self_check = g.step(_self_check, node_id="self_check")
    cloud_direct = g.step(_cloud_direct, node_id="cloud_direct")
    recon = g.step(_recon, node_id="recon")
    outbound_task = g.step(_outbound_task, node_id="outbound_task")
    plan = g.step(_plan, node_id="plan")
    finish_local = g.step(_finish_local, node_id="finish_local")
    finish_cloud = g.step(_finish_cloud, node_id="finish_cloud")
    finish_plan = g.step(_finish_plan, node_id="finish_plan")
    lit = lambda *values: g.match(TypeExpression[Literal[values]])  # noqa: E731
    g.add(
        g.edge_from(g.start_node).to(route),
        g.edge_from(route).to(
            g.decision(node_id="by_route")
            .branch(lit("local", "local_verify", "plan").label("local / local_verify / plan").to(snapshot))
            .branch(lit("cloud").label("cloud").to(cloud_direct))),
        g.edge_from(snapshot).to(
            g.decision(node_id="after_snapshot")
            .branch(lit("execute").label("run locally").to(run_reviewed))
            .branch(lit("plan").label("plan mode").to(recon))),
        g.edge_from(run_reviewed).to(
            g.decision(node_id="after_run")
            .branch(lit("finish").label("done").to(finish_local))
            .branch(lit("self_check").label("local_verify").to(self_check))),
        g.edge_from(self_check).to(
            g.decision(node_id="self_check_outcome")
            .branch(lit("passed").label("passed").to(finish_local))
            .branch(lit("escalate_plan").label("failed, needs workspace").to(recon))
            .branch(lit("escalate_cloud").label("failed, text only").to(cloud_direct))),
        g.edge_from(cloud_direct).to(
            g.decision(node_id="cloud_outcome")
            .branch(lit("done").label("answered").to(finish_cloud))
            .branch(lit("fallback").label("blocked or failed: run locally").to(snapshot))),
        g.edge_from(recon).to(
            g.decision(node_id="recon_outcome")
            .branch(lit("done").to(outbound_task))
            .branch(lit("fallback").label("no cloud key: run locally").to(snapshot))),
        g.edge_from(outbound_task).to(
            g.decision(node_id="outbound_outcome")
            .branch(lit("certified").label("certified").to(plan))
            .branch(lit("fallback").label("not certifiable: run locally").to(snapshot))),
        g.edge_from(plan).to(
            g.decision(node_id="plan_outcome")
            .branch(lit("done", "midway").label("done, or failed after local work").to(finish_plan))
            .branch(lit("fallback").label("failed before local work: run locally").to(snapshot))),
        g.edge_from(finish_local, finish_cloud, finish_plan).to(g.end_node),
    )
    return g.build()


def render(agent: "Hermie") -> str:
    """Mermaid sources of the task graph and the step graph (docs/architecture.md embeds them; `hermie --graph`)."""
    return (agent.task_graph.render(title="Task graph", direction="TB") + "\n\n"
            + agent.step_graph.render(title="Step graph", direction="LR"))
