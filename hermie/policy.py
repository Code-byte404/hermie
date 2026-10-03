"""Routing policy: pure functions, no I/O, easy to unit-test and tune."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .config import Settings
from .judge import ChoiceAnswer, ScoreAnswer


class Route(str, Enum):
    LOCAL = "local"                # local only: just the executor
    LOCAL_VERIFY = "local_verify"  # local + self-check: escalate if the self-check fails (no private data only)
    PLAN = "plan"                  # plan mode: cloud planner + local executor; only redacted text and reports go out
    CLOUD = "cloud"                # no private data, no local files needed: the cloud model answers directly

    @property
    def label(self) -> str:
        return {"local": "local only", "local_verify": "local + self-check",
                "plan": "plan mode", "cloud": "cloud direct"}[self.value]


class Force(str, Enum):
    NONE = "none"
    LOCAL = "local"
    CLOUD = "cloud"


TASK_TYPES = {
    "repetitive": "Simple repetitive or batch work: format conversion, field extraction, translation, "
                  "classification, rewriting, item-by-item summaries, etc.",
    "simple": "A simple single-step task or a common-knowledge question",
    "complex": "A complex task: multi-step reasoning, in-depth analysis, hard code or math, "
               "synthesis of long documents",
    "planning": "Needs overall planning: solution design, project/architecture planning, "
                "strategy making, task breakdown",
}

COMPLEXITY_LEVELS = [
    "Easy: a mid-sized local model can reliably do it well",
    "Medium: needs some reasoning or several steps; a local model may make mistakes",
    "Hard: needs strong reasoning, broad knowledge or global planning; a local model will most likely fail",
]

# Decide "actually build something" vs "just answer". The examples are there because small models are unstable
# on abstract phrasing ("does this require modifying local files"): "build an app" gets a "no" and the cloud
# then only returns a tutorial.
NEEDS_WORKSPACE_QUESTION = (
    "Is the user asking for something to actually be produced -- for example \"build an app\", "
    "\"write a script\", \"make a website\", \"generate/modify files\", \"run a program\" -- "
    "such that files must be created or modified on the computer for the task to count as done?"
)

# Asked only when a data connector is ready. Concrete examples on purpose, like NEEDS_WORKSPACE_QUESTION.
BUSINESS_DATA_QUESTION = (
    "Does answering this need the user's own business data from one of their online accounts -- for example "
    "App Store Connect sales, downloads or reviews, Google Play Console, Google Analytics, Apple Ads or other ad "
    "spend, or revenue reports -- rather than general knowledge?"
)


@dataclass
class Signals:
    sensitive: bool
    task: ChoiceAnswer
    complexity: ScoreAnswer
    win_rate: Optional[float]
    needs_workspace: bool
    needs_workspace_prob: Optional[float] = None  # raw vote share, for the calibration script to tune the threshold
    business: bool = False                        # business data involved (prefix, session or judge): local only
    business_prob: Optional[float] = None         # raw vote share of BUSINESS_DATA_QUESTION (None: not asked)


@dataclass
class Decision:
    route: Route
    reasons: list[str]


def decide(sig: Signals, s: Settings, force: Force = Force.NONE) -> Decision:
    if sig.business:
        reasons = ["business data: local only"]
        if force is Force.CLOUD:
            reasons.append("cloud was requested, but business data never leaves this machine")
        return Decision(Route.LOCAL, reasons)
    reasons: list[str] = []
    if force is Force.LOCAL:
        return Decision(Route.LOCAL, ["user forced local"])

    wants_cloud: Optional[bool] = None  # True / False / None (undecided)
    task, cx, win_rate = sig.task, sig.complexity, sig.win_rate
    task_sure = task.confidence >= s.min_confidence
    cx_sure = cx.confidence >= s.min_confidence
    rl_high = win_rate is not None and win_rate >= s.routellm_threshold
    rl_low = win_rate is not None and win_rate < s.routellm_threshold

    if force is Force.CLOUD:
        wants_cloud = True
        reasons.append("user forced cloud")
    elif task_sure and task.choice == "repetitive":
        wants_cloud = False
        reasons.append("simple repetitive work: always local")
    elif task_sure and task.choice == "planning":
        wants_cloud = True
        reasons.append("overall planning task")
    elif cx_sure and cx.score >= 2:
        wants_cloud = True
        reasons.append("judge: hard")
    elif task_sure and task.choice == "complex" and rl_high:
        wants_cloud = True
        reasons.append(f"complex task and RouteLLM win rate {win_rate:.2f} above threshold")
    elif cx_sure and cx.score == 0 and not rl_high:
        wants_cloud = False
        reasons.append("judge: easy")
    elif task_sure and task.choice == "simple" and rl_low:
        wants_cloud = False
        reasons.append("simple task and RouteLLM says local is enough")
    else:
        reasons.append("signals disagree or confidence too low")

    if sig.sensitive:
        if wants_cloud:
            return Decision(Route.PLAN, reasons + ["private data: the planner only sees a de-identified description; "
                                                   "the material stays local"])
        return Decision(Route.LOCAL, reasons + ["private data: fully local"])

    if wants_cloud is True:
        if sig.needs_workspace:
            return Decision(Route.PLAN, reasons + ["needs local file operations: planner + local executor"])
        return Decision(Route.CLOUD, reasons + ["no local operations needed: the cloud model completes it directly"])
    if wants_cloud is False:
        return Decision(Route.LOCAL, reasons)
    return Decision(Route.LOCAL_VERIFY, reasons + ["local first; escalate if the self-check fails"])
