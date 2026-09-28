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

import logging
import time
from dataclasses import dataclass, field
from functools import wraps
from typing import TYPE_CHECKING, Literal, Optional

from pydantic_graph import GraphBuilder, StepContext, TypeExpression

from .agents import ExecutorOutput, Review, Status
from .audit import sha256
from .events import ReviewArrived
from .session import TaskState

if TYPE_CHECKING:
    from .core import Hermie

log = logging.getLogger(__name__)

# Every decision value a node can return. traced() records these, and only these, as the node status; anything else
# a node returns is recorded as "ok" so that no text ever lands in the trajectory.
DECISIONS = frozenset({"again", "done", "diagnose"})


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
    to fix, up to verify_rounds rounds. Same loop as the old core._execute_reviewed, one iteration per visit."""
    st, agent = ctx.state, ctx.deps
    run: _StepRun = st.step
    out = run.out
    rounds = agent.s.verify_rounds
    decision: Literal["again", "done", "diagnose"] = "done"
    if out.report.status is Status.DONE and run.round < rounds:
        run.round += 1
        rnd = run.round
        review = await agent._review(st, run.inp.task_text, run.inp.acceptance, out)
        if review is not None:
            run.review = review
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
