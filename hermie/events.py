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
    outline: list[str] = field(default_factory=list)  # the overall plan from the planner's set_plan (may exceed what was delegated)


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
class StatsUpdated(Event):
    stats: dict   # Stats.snapshot()


@dataclass
class TaskFinished(Event):
    route: str
    backend: str
    output: str
    outbound_count: int


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


Subscriber = Callable[[Event], None]
Approver = Callable[[ApprovalRequest], Awaitable[Approval]]
InputProvider = Callable[[InputRequest], Awaitable[Optional[str]]]   # None = stop the command


class EventBus:
    def __init__(self):
        self._subs: list[Subscriber] = []
        self.approver: Optional[Approver] = None
        self.input_provider: Optional[InputProvider] = None   # set by an interactive UI; headless runs leave it unset

    def subscribe(self, fn: Subscriber) -> None:
        self._subs.append(fn)

    def emit(self, event: Event) -> None:
        for fn in list(self._subs):
            try:
                fn(event)
            except Exception:
                log.exception("Event subscriber raised an error")

    async def request_approval(self, req: ApprovalRequest) -> Approval:
        if self.approver is None:
            return Approval.DENY  # with no UI to ask, high-risk operations in default mode are always denied
        return await self.approver(req)

    async def request_input(self, req: InputRequest) -> Optional[str]:
        if self.input_provider is None:
            return None
        return await self.input_provider(req)
