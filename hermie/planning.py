"""Plan mode: the planner's design phase (questions, a structured plan, approval) and its execution phase.

The planner is a cloud model. Everything it sees is CleanText (certified by the privacy gate); everything it
produces is restored locally (restore_local) before the user, PLAN.md or the executor see it.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


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
