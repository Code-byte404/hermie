"""Decouples the core from the UI: the core only emits events, the UI subscribes and renders.

Approval and input requests are the only things that flow back: the core awaits the approver's result
(in auto mode it passes straight through) or the text the user types into a command waiting for input.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger(__name__)


@dataclass
class Event:
    ts: float = field(default_factory=time.time, init=False)

    @property
    def event_name(self) -> str:
        return type(self).__name__

    def to_dict(self) -> dict[str, Any]:
        return {"event": self.event_name, **asdict(self)}


@dataclass
class RouteDecided(Event):
    route: str
    reasons: list[str]
    signals: dict


@dataclass
class PlanUpdated(Event):
    steps: list[str]       # steps the planner delegated (the cloud-side text before placeholder restoration is not shown)
    done: list[bool]
    outline: list[str] = field(default_factory=list)  # the overall plan from the planner's submit_plan (may exceed what was delegated)


@dataclass
class ChatMessage(Event):
    role: str   # user / planner / executor / system
    text: str
    streaming: bool = False  # True means an incremental fragment


@dataclass
class CommandStarted(Event):
    tool: str
    summary: str
    risk: Optional[str] = None


@dataclass
class CommandFinished(Event):
    tool: str
    summary: str
    exit_code: int
    output: str
    duration_s: float


@dataclass
class OutboundSent(Event):
    model: str
    content: str   # certified clean content, so the user can check what the gate let through


@dataclass
class OutboundBlocked(Event):
    reason: str


@dataclass
class ReportArrived(Event):
    report: dict
    stripped: bool = False


@dataclass
class Tainted(Event):
    reason: str


@dataclass
class ReviewArrived(Event):
    """The local reviewer's verdict on the executor's result (based on the workspace diff and command
    outputs, not on the executor's own account). Local only."""
    passed: bool
    problems: list[str]
    suggestions: list[str]
    round: int        # round number (1 = after the first execution)
    final: bool       # whether another fix round will follow


@dataclass
class SnapshotTaken(Event):
    snapshot_id: str
    kind: str            # git / clone
    label: str = "task"  # task = before a task; step = before each delegation in plan mode


@dataclass
class Notice(Event):
    level: str  # info / warn / error
    text: str


@dataclass
class ExecutorProgress(Event):
    """Heartbeat of a running executor step (every few seconds), so the UI can show that the local model is still
    working, what it did last and when the step will be stopped. done=True: the step ended."""
    elapsed_s: float
    tool_calls: int
    last_tool: str          # tool name only
    since_tool_s: float     # seconds since the last tool call finished (or since the step started)
    limit_s: float          # the step is stopped when elapsed_s reaches this (time spent waiting for the user excluded)
    done: bool = False
    started: bool = False   # first beat of a step


@dataclass
class StatsUpdated(Event):
    stats: dict   # Stats.snapshot()


@dataclass
class TaskFinished(Event):
    route: str
    backend: str
    output: str
    outbound_count: int
    status: str = ""        # the executor report's status (done / partial / failed / needs_input), "" if none
    elapsed_s: float = 0.0


@dataclass
class PlanProposed(Event):
    """A plan the planner submitted was accepted (by the user or automatically); restored local text."""
    plan: dict
    revision: int
    approved_by: str   # user / auto


class Approval(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ALLOW_SESSION = "allow_session"


@dataclass
class ApprovalRequest:
    tool: str
    summary: str
    risk: str
    reason: str


@dataclass
class InputRequest:
    """A running command went quiet on a prompt: the user types the answer (a line) or stops the command."""
    command: str
    output: str   # tail of the command's output, ending with the prompt


@dataclass
class QuestionView:
    question: str
    options: list[str]   # the recommended option first
    why: str = ""


@dataclass
class ClarifyRequest:
    """The planner asks the user (restored local text). timeout_s: answer with the recommended options after this."""
    round: int
    questions: list[QuestionView]
    timeout_s: Optional[float] = None


@dataclass
class ClarifyAnswer:
    option: Optional[int] = None   # 0-based index into QuestionView.options
    text: Optional[str] = None     # free text ("Other"); both None = skipped


@dataclass
class PlanReviewRequest:
    plan: dict           # restored Plan.model_dump()
    markdown: str        # plan_doc.render() of it
    revision: int        # 1 = first submission
    changed: dict        # plan_doc.diff_steps() against the previous submission
    final: bool = False  # no more change requests possible: approve or reject


@dataclass
class PlanDecision:
    action: str          # approve / revise / reject
    feedback: str = ""   # the change request (local text; goes out only through the gate)


@dataclass
class ChoiceRequest:
    """A connector needs the user to pick one option (an app, an Apple Ads organization). Local text only."""
    prompt: str
    options: list[str]


Subscriber = Callable[[Event], None]
Approver = Callable[[ApprovalRequest], Awaitable[Approval]]
InputProvider = Callable[[InputRequest], Awaitable[Optional[str]]]   # None = stop the command
Clarifier = Callable[[ClarifyRequest], Awaitable[Optional[list[ClarifyAnswer]]]]   # None = stop the task
PlanReviewer = Callable[[PlanReviewRequest], Awaitable[PlanDecision]]
Chooser = Callable[[ChoiceRequest], Awaitable[Optional[str]]]   # None = cancelled


class EventBus:
    def __init__(self):
        self._subs: list[Subscriber] = []
        self.approver: Optional[Approver] = None
        self.input_provider: Optional[InputProvider] = None   # set by an interactive UI; headless runs leave it unset
        self.clarifier: Optional[Clarifier] = None         # planner questions; headless runs leave it unset
        self.plan_reviewer: Optional[PlanReviewer] = None  # plan approval; unset = approve automatically
        self.chooser: Optional[Chooser] = None   # connector pickers; headless runs leave it unset
        self._user_wait_s = 0.0   # time spent waiting for approvals / input (not charged to the executor's time limit)
        self._waiting_since: Optional[float] = None

    def subscribe(self, fn: Subscriber) -> None:
        self._subs.append(fn)

    def emit(self, event: Event) -> None:
        for fn in list(self._subs):
            try:
                fn(event)
            except Exception:
                log.exception("Event subscriber raised an error")

    def user_wait_s(self) -> float:
        """Total time spent waiting for the user so far, including a wait in progress."""
        now = time.monotonic()
        return self._user_wait_s + (now - self._waiting_since if self._waiting_since is not None else 0.0)

    async def _wait_for_user(self, coro):
        if self._waiting_since is not None:   # nested / concurrent waits: the outer one already counts
            return await coro
        self._waiting_since = time.monotonic()
        try:
            return await coro
        finally:
            self._user_wait_s += time.monotonic() - self._waiting_since
            self._waiting_since = None

    async def request_approval(self, req: ApprovalRequest) -> Approval:
        if self.approver is None:
            return Approval.DENY  # with no UI to ask, high-risk operations in default mode are always denied
        return await self._wait_for_user(self.approver(req))

    async def request_input(self, req: InputRequest) -> Optional[str]:
        if self.input_provider is None:
            return None
        return await self._wait_for_user(self.input_provider(req))

    async def request_clarification(self, req: ClarifyRequest) -> Optional[list[ClarifyAnswer]]:
        if self.clarifier is None:
            return None
        return await self._wait_for_user(self.clarifier(req))

    async def request_plan_review(self, req: PlanReviewRequest) -> PlanDecision:
        if self.plan_reviewer is None:
            return PlanDecision("approve")
        return await self._wait_for_user(self.plan_reviewer(req))

    async def request_choice(self, req: ChoiceRequest) -> Optional[str]:
        if self.chooser is None:
            return None
        return await self._wait_for_user(self.chooser(req))
