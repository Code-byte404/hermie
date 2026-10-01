"""Plan mode: the planner's design phase (questions, a structured plan, approval) and its execution phase.

The planner is a cloud model. Everything it sees is CleanText (certified by the privacy gate); everything it
produces is restored locally (restore_local) before the user, PLAN.md or the executor see it.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Optional

from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry, RunContext, Tool, ToolOutput, UsageLimits

from . import plan_doc
from .agents import ModelFactory, Status, format_report, restore_local
from .capabilities import OutboundGuard, PlannerToolBudget
from .config import RunMode, Settings
from .events import (ClarifyAnswer, ClarifyRequest, Notice, PlanProposed, PlanReviewRequest, PlanUpdated,
                     QuestionView, ReportArrived)
from .mactools import xcode_available
from .privacy import CleanText, PrivacyGate
from .session import TaskState

if TYPE_CHECKING:
    from .core import Hermie

log = logging.getLogger(__name__)


class PlanStep(BaseModel):
    title: str = Field(description="One line; shown in the plan panel and as the PLAN.md checkbox")
    details: str = Field(description="What to do and how: approach, components touched")
    files: list[str] = Field(default_factory=list, description="Files this step creates or changes")
    acceptance: list[str] = Field(description="1-5 concrete, checkable conditions")
    depends_on: list[int] = Field(default_factory=list, description="Numbers (1-based) of earlier steps this needs")


class Plan(BaseModel):
    goal: str = Field(description="The task restated: what done looks like for the user")
    decisions: list[str] = Field(default_factory=list, description="What the user answered, as decisions")
    assumptions: list[str] = Field(default_factory=list,
                                   description="What you decided without asking; the user can correct these")
    architecture: str = Field(description="Components, stack, data flow, where data comes from")
    steps: list[PlanStep]
    risks: list[str] = Field(default_factory=list, description="What may go wrong and how the plan handles it")
    out_of_scope: list[str] = Field(default_factory=list)


class Question(BaseModel):
    question: str
    options: list[str] = Field(description="2-4 options, the recommended one first")
    why: str = Field(default="", description="One line: what the answer changes in the plan")


def plan_problems(plan: Plan, max_steps: int) -> list[str]:
    """Why a submitted plan cannot be accepted as is (empty list = fine). Sent back to the planner as a retry."""
    out = []
    if not plan.steps:
        out.append("the plan has no steps")
    if len(plan.steps) > max_steps:
        out.append(f"the plan may have at most {max_steps} steps; merge steps")
    for i, step in enumerate(plan.steps, 1):
        if not 1 <= len(step.acceptance) <= 5:
            out.append(f"step {i} needs 1-5 acceptance criteria")
        if any(not 1 <= d < i for d in step.depends_on):
            out.append(f"step {i} may only depend on earlier steps (1..{i - 1})")
    return out


def question_problems(questions: list[Question]) -> list[str]:
    out = []
    if not 1 <= len(questions) <= 4:
        out.append("ask 1-4 questions per round")
    for i, q in enumerate(questions, 1):
        if not 2 <= len(q.options) <= 4:
            out.append(f"question {i} needs 2-4 options, the recommended one first")
    return out


PLANNER_COMMON = """You are the planner. The task description you see has been de-identified: placeholders of the form <ENTITY_n>
and "file#n" stand for hidden specifics; keep them exactly as they are, they are restored automatically on the local side.
The task description may be followed by [Project doc AGENT.md], [Existing plan] and [Workspace overview] (directory layout,
project type, toolchain); use them to judge the current state first and do not redo work that is already finished.
The executor is a smaller local model that can run commands and read/write workspace files in a sandbox with network access
(python, pandoc etc. are available). You never see file contents, and you must not ask the executor to send raw data to you.
Step numbers (plan_step, depends_on) start at 1."""

DESIGN_INSTRUCTIONS = PLANNER_COMMON + """
You are in the design phase: understand the task, settle what is ambiguous, then submit a detailed plan with submit_plan.
You cannot delegate yet; execution starts after the user approved the plan.
- Read the overview, AGENT.md and any existing plan first: decide whether this is a new project or a change to an existing one.
  Never ask what the overview already answers. If there is an [Existing plan], first ask whether to continue it or start over.
- Ask (ask_user) only questions whose answer changes the architecture or the scope: target platform, data source, the MVP
  feature set, offline behaviour, accounts, deployment target. Anything with a sensible default (code style, layout, test
  framework) goes under assumptions instead of being asked.
- 1-4 questions per round, 2-4 options each, the recommended option first, with a one-line why. Ask a follow-up round only
  when an answer opened a new decision. A clear, small task gets no questions and a short plan (1-2 steps).
- When ask_user is not available, decide yourself and list every uncertainty under assumptions.
- The plan: goal (what done looks like), decisions (the user's answers), assumptions, architecture (components, stack, data
  flow, where data comes from), steps, risks, out_of_scope. Steps are independently verifiable increments: first a skeleton
  that builds and runs, then one feature per step, the last step an overall acceptance check. Each step names its files and
  has 1-5 concrete acceptance criteria.
- If submit_plan comes back with the user's change requests, revise and submit again."""

EXECUTE_INSTRUCTIONS = PLANNER_COMMON + """
The plan was approved by the user. Execute it:
- delegate(step, acceptance, plan_step): hand one step to the local executor; give plan_step (1-based) for a planned step,
  its acceptance criteria are used when you give none. A fix-up delegation outside the plan has no plan_step.
  A local reviewer checks the actual workspace changes against acceptance; if it fails, the executor must fix things before reporting.
- You receive a structured report: status, steps_done, artifacts, verification, issues, question, local_review and diagnosis.
- Reports include the remaining delegation budget: fit verification and fixes into it; if the same step fails twice, change
  approach or narrow the scope instead of retrying as is.
- ask_user only when you are blocked on a decision the user must make (missing credentials, a fork the plan did not foresee).
- revise_plan(plan) when steps must be added, removed or substantially changed; the user approves the revision.
When everything is finished, summarize briefly in English what was done, which files were produced and which acceptance checks passed."""

PLANNER_WEB_NOTE = ("\nThe executor has web access: web_search for the latest information, web_fetch to read a page's text. "
                    "When up-to-date data is needed, delegate the lookup to it and require it to cite sources.")
PLANNER_MAC_NOTE = ("\nThe executor runs on a Mac with the Xcode toolchain: it can build Xcode projects and Swift packages, boot the "
                    "iOS simulator, install and launch apps, drive the UI (tap, type, swipe) and take screenshots that it can look "
                    "at itself. For app work, delegate build / run / visual check steps and require a screenshot-based check.")


class PlanRejected(Exception):
    """The user rejected the plan: nothing is executed."""


def _notes(models: ModelFactory) -> str:
    out = PLANNER_WEB_NOTE if models.s.web_enabled else ""
    if models.s.mac_tools and xcode_available():
        out += PLANNER_MAC_NOTE
    return out


def localize_plan(st: TaskState, plan: Plan) -> Plan:
    """The plan with placeholders and file#n restored (for the user, PLAN.md and the executor; never sent)."""
    r = lambda x: restore_local(st, x)  # noqa: E731
    return Plan(goal=r(plan.goal), decisions=[r(x) for x in plan.decisions], assumptions=[r(x) for x in plan.assumptions],
                architecture=r(plan.architecture), risks=[r(x) for x in plan.risks],
                out_of_scope=[r(x) for x in plan.out_of_scope],
                steps=[PlanStep(title=r(s.title), details=r(s.details), files=[r(f) for f in s.files],
                                acceptance=[r(a) for a in s.acceptance], depends_on=list(s.depends_on))
                       for s in plan.steps])


def delegation_limit_for(s: Settings, steps: int) -> int:
    return min(s.plan_delegation_cap, max(s.max_delegations, 2 * steps))


def planner_usage_limits(s: Settings, budget: int) -> UsageLimits:
    extra = s.plan_max_question_rounds + s.plan_max_revisions + 10
    return UsageLimits(request_limit=max(s.max_requests, budget + extra), tool_calls_limit=budget + extra)


def budget_line(st: TaskState) -> str:
    return f"Remaining delegations: {max(0, st.delegation_limit - st.delegations)}"


async def answers_text(st: TaskState, host: "Hermie", questions: list[Question],
                       answers: Optional[list[ClarifyAnswer]]) -> CleanText:
    """The certified tool result for one round of answers. A chosen option goes out as the planner's own (cloud-side)
    text; free text takes the outbound ladder; skipped / missing / withheld answers are fixed templates."""
    answers = list(answers or [])
    lines: list[str] = []
    for i, q in enumerate(questions, 1):
        a = answers[i - 1] if i - 1 < len(answers) else ClarifyAnswer()
        if a.option is not None and 0 <= a.option < len(q.options):
            lines.append(f"Q{i}: {q.options[a.option]}")
        elif a.text and a.text.strip():
            notes: list[str] = []
            clean, how = await host._certify_outbound(st, a.text.strip(), notes)
            if clean is None:
                st.answers_withheld += 1
                st.sensitive_input = True
                st.bus.emit(Notice("warn", f"Your answer to Q{i} was withheld: it could not be sent without private data"))
                lines.append(f"Q{i}: answer withheld (private). Decide yourself and list it under assumptions.")
            else:
                if how != "original":
                    st.answers_redacted += 1
                    st.sensitive_input = True
                    st.bus.emit(Notice("info", f"Your answer to Q{i} was sent as: {restore_preview(clean.text)}"))
                lines.append(f"Q{i}: {clean.text}")
        else:
            lines.append(f"Q{i}: user skipped; decide yourself and list it under assumptions.")
    try:
        return st.remember(await asyncio.to_thread(st.gate.certify, "\n".join(lines)))
    except PermissionError:   # the combination did not certify: integers and fixed text only
        safe = [f"Q{i}: option {answers[i - 1].option + 1}" if i - 1 < len(answers) and answers[i - 1].option is not None
                else f"Q{i}: user skipped; decide yourself and list it under assumptions."
                for i in range(1, len(questions) + 1)]
        return st.remember(PrivacyGate.trusted_template("\n".join(safe)))


def restore_preview(text: str) -> str:
    """What was sent, shown to the user as is (placeholders visible): the point is to see what left."""
    return text if len(text) <= 200 else text[:200] + "..."


async def ask(st: TaskState, host: "Hermie", questions: list[Question]) -> str:
    if problems := question_problems(questions):
        raise ModelRetry("; ".join(problems))
    st.question_rounds += 1
    st.questions_asked += len(questions)
    views = [QuestionView(restore_local(st, q.question), [restore_local(st, o) for o in q.options],
                          restore_local(st, q.why)) for q in questions]
    timeout = st.s.plan_auto_answer_s if st.s.mode is not RunMode.DEFAULT else None
    try:
        # None = the user stopped the task; the TUI then cancels the task worker itself (action_interrupt), so the
        # cancellation arrives here as CancelledError. Until it does, treat it like "all skipped".
        answers = await st.bus.request_clarification(ClarifyRequest(st.question_rounds, views, timeout))
    except asyncio.CancelledError:
        raise
    except Exception:   # a broken dialog must not stop the task: the planner decides itself
        log.exception("Clarification dialog failed")
        answers = []
    return (await answers_text(st, host, questions, answers)).text


async def review_plan(st: TaskState, host: "Hermie", plan: Plan, revising: bool = False) -> tuple[str, Optional[str]]:
    """Validate, show, and get a decision on a submitted plan. Returns ("approve", None), ("revise", certified
    feedback) or ("reject", None). On approve the plan becomes the task's plan (state, PLAN.md, events).
    revising: called from revise_plan, whose approval is counted in plan_revisions only after this returns."""
    if problems := plan_problems(plan, st.s.plan_max_steps):
        raise ModelRetry("; ".join(problems))
    local = localize_plan(st, plan)
    # The proposal's number: every change request answered and every approved revise_plan counts one
    revision = st.plan_revisions + (2 if revising and st.plan_local is not None else 1)
    ask_user = st.s.mode is RunMode.DEFAULT and st.bus.plan_reviewer is not None
    action, feedback = "approve", ""
    if ask_user:
        final = st.plan_revisions >= st.s.plan_max_revisions
        req = PlanReviewRequest(local.model_dump(), plan_doc.render(local, []), revision,
                                plan_doc.diff_steps(st.plan_local, local), final)
        try:
            decision = await st.bus.request_plan_review(req)
            action, feedback = decision.action, decision.feedback
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Plan review dialog failed; approving")
            action = "approve"
        if action == "revise" and final:
            action = "approve"
    if action == "reject":
        return "reject", None
    if action == "revise":
        st.plan_revisions += 1
        clean, how = await host._certify_outbound(st, feedback.strip() or "Please improve the plan.", [])
        out = None
        if clean is not None:
            try:
                # remember the exact string that goes out (as a ModelRetry / tool result), so OutboundGuard sees a known hash
                out = st.remember(await asyncio.to_thread(st.gate.certify, f"The user asked for changes:\n{clean.text}"))
            except PermissionError:
                out = None
        if out is None:
            st.answers_withheld += 1
            st.sensitive_input = True
            st.bus.emit(Notice("warn", "Your change request was withheld: it could not be sent without private data"))
            return "revise", st.remember(PrivacyGate.trusted_template(
                "The user asked for changes that could not be sent (private). Ask a multiple-choice question instead.")).text
        if how != "original":
            st.answers_redacted += 1
            st.sensitive_input = True
            st.bus.emit(Notice("info", f"Your change request was sent as: {restore_preview(clean.text)}"))
        return "revise", out.text
    _accept(st, plan, local, "user" if ask_user else "auto", revision)
    return "approve", None


def _accept(st: TaskState, plan: Plan, local: Plan, approved_by: str, revision: int) -> None:
    old = st.plan_local
    st.plan, st.plan_local, st.plan_approved_by = plan, local, approved_by
    if old is None:
        st.plan_step_done = [False] * len(plan.steps)
    else:   # keep ticks of steps whose title survived the revision
        before = {s.title: d for s, d in zip(old.steps, st.plan_step_done)}
        st.plan_step_done = [before.get(s.title, False) for s in local.steps]
    st.delegation_budget = delegation_limit_for(st.s, len(plan.steps))
    st.plan_outline = [s.title for s in local.steps]
    st.bus.emit(PlanUpdated(list(st.plan_steps), list(st.plan_done), list(st.plan_outline)))
    st.bus.emit(PlanProposed(local.model_dump(), revision, approved_by))
    write_plan_file(st)


def write_plan_file(st: TaskState) -> None:
    if st.plan_local is None:
        return
    try:
        if st.plan_path is None:
            st.plan_path = plan_doc.target(st.s.workspace, st.s.plan_file)
        plan_doc.write(st.plan_path, st.plan_local, st.plan_step_done)
    except OSError as e:
        st.bus.emit(Notice("warn", f"Could not write the plan file: {e}"))


async def _ask_user(ctx: RunContext[TaskState], questions: list[Question]) -> str:
    """Ask the user 1-4 multiple-choice questions (2-4 options each, the recommended option first, a one-line why).
    Returns their answers as "Qn: ..." lines."""
    return await ask(ctx.deps, ctx.deps.host, questions)


def build_designer(models: ModelFactory, host: "Hermie", can_ask: bool) -> Agent[TaskState, Plan]:
    """The design phase: questions (when can_ask) and submit_plan, the run's output. Approval happens in the output
    validator: approve ends the run, a change request is a ModelRetry with the certified feedback, reject raises
    PlanRejected."""
    model = models.planner()
    agent = Agent(model, deps_type=TaskState, name="designer",
                  output_type=ToolOutput(Plan, name="submit_plan",
                                         description="Submit the detailed plan for the user's approval",
                                         max_retries=models.s.plan_max_revisions + 2),
                  instructions=DESIGN_INSTRUCTIONS + _notes(models),
                  tools=[Tool(_ask_user, name="ask_user", sequential=True)] if can_ask else [],
                  capabilities=[OutboundGuard(model_name=model.model_name), PlannerToolBudget(),
                                *models.tracker("designer", "cloud")])

    @agent.output_validator
    async def _review(ctx: RunContext[TaskState], plan: Plan) -> Plan:
        action, feedback = await review_plan(ctx.deps, host, plan)
        if action == "reject":
            raise PlanRejected()
        if action == "revise":
            raise ModelRetry(feedback)
        return plan

    return agent


def build_planner(models: ModelFactory, host: "Hermie", can_ask: bool) -> Agent[TaskState, str]:
    """The execution phase. With the design phase off (PLAN_DESIGN=false) it also offers submit_plan, auto-approved.
    can_ask: an interactive UI is attached and the task did not arrive here by escalation."""
    model = models.planner()

    async def submit_plan(ctx: RunContext[TaskState], plan: Plan) -> str:
        """Record the overall plan. Then delegate step by step."""
        st = ctx.deps
        if problems := plan_problems(plan, st.s.plan_max_steps):
            raise ModelRetry("; ".join(problems))
        _accept(st, plan, localize_plan(st, plan), "auto", st.plan_revisions + 1)
        return st.remember(PrivacyGate.trusted_template(
            f"Plan recorded, {len(plan.steps)} steps. {budget_line(st)}")).text

    async def revise_plan(ctx: RunContext[TaskState], plan: Plan) -> str:
        """Replace the approved plan (steps added, removed or substantially changed); the user approves it."""
        st = ctx.deps
        action, feedback = await review_plan(st, host, plan, revising=True)
        if action == "approve":
            st.plan_revisions += 1
            return st.remember(PrivacyGate.trusted_template(f"Revision approved. {budget_line(st)}")).text
        if action == "revise":
            return feedback
        return st.remember(PrivacyGate.trusted_template(
            "The user rejected the revision: keep the approved plan, or stop and summarize.")).text

    async def delegate(ctx: RunContext[TaskState], step: str, acceptance: Optional[list[str]] = None,
                       plan_step: Optional[int] = None) -> str:
        """Delegate one concrete step to the local executor. plan_step: the 1-based plan step this carries out
        (its acceptance criteria are used when acceptance is empty). Returns the structured report."""
        st = ctx.deps
        st.delegations += 1
        planned = st.plan is not None and plan_step is not None and 1 <= plan_step <= len(st.plan.steps)
        if planned and not acceptance:
            acceptance = st.plan.steps[plan_step - 1].acceptance
        local_step = restore_local(st, step)
        local_acc = [restore_local(st, a) for a in (acceptance or [])]
        st.plan_steps.append(local_step)
        st.plan_done.append(False)
        st.bus.emit(PlanUpdated(list(st.plan_steps), list(st.plan_done), list(st.plan_outline)))
        try:
            out, review, diagnosis = await host._delegated_step(st, local_step, local_acc,
                                                                plan_step if planned else None)
            report = out.report
        except Exception as e:
            log.exception("Executor run failed")
            st.bus.emit(Notice("error", f"Executor run failed: {e}"))
            return st.remember(PrivacyGate.trusted_template(
                f"status: failed\nissues:\n- executor run failed ({type(e).__name__})\n{budget_line(st)}")).text
        passed = report.status is Status.DONE and (review is None or review.passed)
        st.plan_done[-1] = passed
        if planned and passed:
            st.plan_step_done[plan_step - 1] = True
            write_plan_file(st)
        st.bus.emit(PlanUpdated(list(st.plan_steps), list(st.plan_done), list(st.plan_outline)))
        candidates = [(format_report(report, review, diagnosis), {"review": review, "diagnosis": diagnosis}),
                      (format_report(report, review), {"review": review}),
                      (format_report(report), {})]
        clean, shown = None, {}
        for txt, extra in candidates:
            try:
                clean = await asyncio.to_thread(st.gate.certify, f"{txt}\n{budget_line(st)}")
            except PermissionError:
                continue
            shown = report.model_dump(mode="json", exclude_none=True)
            if extra.get("review") is not None:
                shown["local_review"] = extra["review"].model_dump()
            if extra.get("diagnosis"):
                shown["diagnosis"] = extra["diagnosis"]
            break
        if clean is None:  # last resort: keep only status
            st.report_stripped = True
            clean = PrivacyGate.trusted_template(f"status: {report.status.value}\n{budget_line(st)}")
            shown = {"status": report.status.value}
        st.bus.emit(ReportArrived(shown, st.report_stripped))
        return st.remember(clean).text

    tools = [Tool(delegate, sequential=True), Tool(revise_plan, sequential=True)]
    if not models.s.plan_design:
        tools.append(Tool(submit_plan, sequential=True))
    if can_ask:
        tools.append(Tool(_ask_user, name="ask_user", sequential=True))
    instructions = (EXECUTE_INSTRUCTIONS if models.s.plan_design else
                    EXECUTE_INSTRUCTIONS + "\nFirst record a plan with submit_plan (it is accepted as is), then delegate.")
    return Agent(model, deps_type=TaskState, output_type=str, instructions=instructions + _notes(models),
                 name="planner", tools=tools,
                 capabilities=[OutboundGuard(model_name=model.model_name), PlannerToolBudget(),
                               *models.tracker("planner", "cloud")])
