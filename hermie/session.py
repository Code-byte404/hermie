"""Session and task state. Session lives across tasks; TaskState is one per task and is the deps for both agents."""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from .audit import AuditLog, JsonlLog, sha256
from .config import RunMode, Settings
from .events import EventBus
from .policy import Force

if TYPE_CHECKING:
    from .judge import Judge
    from .mactools import ScreenCapture
    from .web import WebClient
    from .privacy import CleanText, PrivacyGate
    from .sandbox import Sandbox
    from .snapshot import SnapshotManager


@dataclass
class Stats:
    """Session statistics: cloud/local tokens, agent run counts, the model currently being awaited, elapsed time.
    The judge model reports from a thread pool, hence the lock; every change notifies the UI via on_change."""
    started_at: float = field(default_factory=time.time)
    cloud: dict = field(default_factory=lambda: {"requests": 0, "input_tokens": 0, "output_tokens": 0})
    local: dict = field(default_factory=lambda: {"requests": 0, "input_tokens": 0, "output_tokens": 0})
    judge_requests: int = 0
    web: dict = field(default_factory=lambda: {"search": 0, "fetch": 0})
    runs: dict = field(default_factory=dict)      # role -> number of agent runs started
    active: dict = field(default_factory=dict)    # run key -> role (agents currently running)
    current: Optional[str] = None                 # model currently being awaited, e.g. "☁ deepseek-v4-pro"
    current_since: Optional[float] = None
    current_role: str = ""                        # agent role of the request being awaited (executor, planner, ...)
    replies: list = field(default_factory=list)   # recent finished agent requests, for the UI log (seq, role, model, secs, tokens)
    replies_seq: int = 0
    task_started_at: Optional[float] = None
    on_change: Optional[Callable[[], None]] = None
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def _changed(self) -> None:
        if self.on_change:
            self.on_change()

    def run_started(self, role: str, key: str) -> None:
        with self._lock:
            self.runs[role] = self.runs.get(role, 0) + 1
            self.active[key] = role
        self._changed()

    def run_finished(self, key: str) -> None:
        with self._lock:
            self.active.pop(key, None)
        self._changed()

    def request_started(self, where: str, model: str, role: str = "") -> None:
        with self._lock:
            self.current = f"{'☁' if where == 'cloud' else '🔒'} {model}"
            self.current_since = time.time()
            self.current_role = role
        self._changed()

    def request_finished(self, where: str, input_tokens: int, output_tokens: int, *, judge: bool = False) -> None:
        with self._lock:
            d = self.cloud if where == "cloud" else self.local
            d["requests"] += 1
            d["input_tokens"] += input_tokens or 0
            d["output_tokens"] += output_tokens or 0
            if judge:
                self.judge_requests += 1
            else:
                if self.current and self.current_since:
                    self.replies_seq += 1
                    self.replies.append({"seq": self.replies_seq, "role": self.current_role, "model": self.current,
                                         "secs": round(time.time() - self.current_since, 1),
                                         "in": input_tokens or 0, "out": output_tokens or 0})
                    del self.replies[:-20]
                self.current = self.current_since = None
                self.current_role = ""
        self._changed()

    def web_started(self, kind: str, label: str) -> None:
        with self._lock:
            self.current = f"🌐 {'search' if kind == 'search' else 'fetch'}: {label[:60]}"
            self.current_since = time.time()
        self._changed()

    def web_finished(self, kind: str) -> None:
        with self._lock:
            self.web[kind] = self.web.get(kind, 0) + 1
            self.current = self.current_since = None
        self._changed()

    def request_aborted(self) -> None:
        with self._lock:
            self.current = self.current_since = None
            self.current_role = ""
        self._changed()

    def judge_usage(self, input_tokens: int, output_tokens: int) -> None:
        self.request_finished("local", input_tokens, output_tokens, judge=True)

    def snapshot(self) -> dict:
        with self._lock:
            now = time.time()
            return {"cloud": dict(self.cloud), "local": dict(self.local), "judge_requests": self.judge_requests,
                    "web": dict(self.web),
                    "runs": dict(self.runs), "active": list(self.active.values()), "current": self.current,
                    "current_role": self.current_role, "replies": [dict(r) for r in self.replies],
                    "current_elapsed_s": round(now - self.current_since, 1) if self.current_since else None,
                    "session_elapsed_s": round(now - self.started_at, 1),
                    "task_elapsed_s": round(now - self.task_started_at, 1) if self.task_started_at else None}


@dataclass
class Session:
    settings: Settings
    judge: "Judge"
    gate: "PrivacyGate"
    sandbox: "Sandbox"
    snapshots: "SnapshotManager"
    bus: EventBus
    audit: AuditLog
    outbound_log: JsonlLog
    command_log: JsonlLog
    # Local executor history kept across delegations and tasks (compressed by a local model when too long; never sent to the cloud)
    exec_history: list[Any] = field(default_factory=list)
    # Commands allowed for the whole session (by the command's first word)
    session_allow: set[str] = field(default_factory=set)
    outbound_total: int = 0
    stats: Stats = field(default_factory=Stats)
    web: Optional["WebClient"] = None
    screen: Optional["ScreenCapture"] = None   # screenshot tool (main process); None when MAC_TOOLS=false
    review_log: Optional[JsonlLog] = None   # local review records (contain local content; data_dir only)
    trajectory_log: Optional[JsonlLog] = None   # per-task node trajectory (data-free; data_dir only)
    lessons: Optional[Any] = None                 # memory.LessonStore (local lesson memory; data_dir only)
    lessons_notice_sent: bool = False             # "embeddings unavailable" is said once per session
    skills: Optional[Any] = None                  # skills.SkillStore (local skill library; data_dir only)

    @property
    def mode(self) -> RunMode:
        return self.settings.mode


@dataclass
class FlowState:
    """Working memory of the task graph (graph.py): what the nodes hand to each other. Local only."""
    task: str = ""                                # the task line as typed (without material), for RouteLLM and the user echo
    force: Force = Force.NONE
    routing: Optional[Any] = None                 # router.Routing once the route node ran
    task_snapshot_id: Optional[str] = None        # the pre-task snapshot: TaskResult.snapshot_id and rollback target
    notes: list[str] = field(default_factory=list)   # framework-generated reasons in order (self-check note, plan notes, fallbacks)
    fallback: bool = False                        # a cloud step failed and the task is finishing locally
    escalated: bool = False                       # local_verify handed the task to the cloud
    recon: str = ""                               # workspace recon text for the planner (goes through the gate in _outbound_task)
    outbound: Optional[Any] = None                # CleanText the planner receives
    plan_summary: Optional[str] = None            # planner's final text, restored locally
    cloud_output: Optional[str] = None            # cloud-direct answer
    last_step: Optional[Any] = None               # graph.StepResult of the last run_reviewed
    design_messages: Optional[list] = None        # the design run's all_messages(); the plan node continues from them
    plan_rejected: bool = False                   # the user rejected the plan: nothing was executed


@dataclass
class TaskState:
    session: Session
    text: str                         # local original text: task + material
    project_doc: str = ""             # contents of the workspace AGENT.md (local context; goes through the gate before outbound as usual)
    report_for_cloud: bool = False    # in plan mode the executor's report is sent to the planner
    certified: set[str] = field(default_factory=set)  # hashes of certified CleanText
    logged_outbound: set[str] = field(default_factory=set)
    outbound_count: int = 0
    tainted: bool = False             # the executor read sensitive content through a tool
    sensitive_input: bool = False     # the task text / material itself contains sensitive content (decided at routing)
    # Background taint / stuck checks in flight (the judge model is slow and must not block tool returns; settled before outbound and at task end)
    pending_checks: list = field(default_factory=list)
    mapping: dict[str, str] = field(default_factory=dict)  # placeholder -> original text (local only)
    path_aliases: dict[str, str] = field(default_factory=dict)  # file#n -> real path (local only)
    tool_calls: int = 0
    recent_calls: list[str] = field(default_factory=list)
    tool_seq: list[str] = field(default_factory=list)   # tool call order within this executor run (for the verification check)
    last_tool_at: float = 0.0                           # time.monotonic() of the last finished tool call (progress display)
    changed_paths: list[str] = field(default_factory=list)  # files written / edited in this executor run (local UI only)
    stuck: bool = False
    snapshot_id: Optional[str] = None                   # the reviewer diffs the workspace against this (pre-task snapshot; in plan mode the pre-step snapshot)
    route: str = ""
    review_failures: int = 0                            # number of failed reviews in this task
    review_fixed: bool = False                          # a failed review was later fixed successfully (basis for writing a lesson)
    last_review: Optional[dict] = None
    review_history: list[dict] = field(default_factory=list)
    plan_outline: list[str] = field(default_factory=list)  # overall plan given by the planner's submit_plan
    delegations: int = 0
    report_stripped: bool = False
    plan_steps: list[str] = field(default_factory=list)
    plan_done: list[bool] = field(default_factory=list)
    plan: Optional[Any] = None                    # planning.Plan as the planner wrote it (placeholders kept)
    plan_local: Optional[Any] = None              # the same, restored locally (UI, PLAN.md, executor)
    plan_proposed_local: Optional[Any] = None     # the last plan shown for approval (diff base during design)
    plan_step_done: list[bool] = field(default_factory=list)   # per plan step: delegated and passed review
    plan_revisions: int = 0                       # change requests answered + revise_plan calls
    question_rounds: int = 0
    questions_asked: int = 0
    answers_redacted: int = 0                     # answers that went out redacted or abstracted
    answers_withheld: int = 0
    plan_approved_by: str = ""                    # user / auto
    delegation_budget: int = 0                    # 0 = settings.max_delegations (no plan yet)
    plan_path: Optional[Path] = None              # where PLAN.md was written
    host: Optional[Any] = None                    # the Hermie instance (planner tools call its helpers)
    answers: list[str] = field(default_factory=list)
    artifacts: list[dict] = field(default_factory=list)
    last_report: Optional[dict] = None
    # Trajectory: one record per graph node (graph.py `traced`), written by trajectory.task_record at task end.
    # Data-free by construction: counts, booleans, numbers and enum strings only.
    trace: list[dict] = field(default_factory=list)
    _trace_pending: dict = field(default_factory=dict)
    step: Optional[Any] = None        # graph._StepRun while the step graph runs (execute -> review -> fix loop)
    flow: FlowState = field(default_factory=FlowState)
    lessons: list = field(default_factory=list)          # memory.Lesson recalled for the current step
    lessons_used: set[str] = field(default_factory=set)  # ids of every lesson injected during this task
    tools_used: set[str] = field(default_factory=set)    # tool names the executor called during this task (lesson tags)
    problem_counts: dict = field(default_factory=dict)   # problem_key -> [times raised, first sentence]; cleared on a pass
    skill_episodes: list = field(default_factory=list)   # reviewed successes captured in memory for skill distillation
    skills: list = field(default_factory=list)           # skills.Skill recalled for the current step
    skills_used: set[str] = field(default_factory=set)   # ids of every skill injected during this task

    @property
    def s(self) -> Settings:
        return self.session.settings

    @property
    def gate(self) -> "PrivacyGate":
        return self.session.gate

    @property
    def bus(self) -> EventBus:
        return self.session.bus

    @property
    def exposed(self) -> bool:
        """Whether the executor has touched sensitive content (task text contained private data, or a tool read some).
        The web outbound policy tightens based on this."""
        return self.tainted or self.sensitive_input

    @property
    def delegation_limit(self) -> int:
        return self.delegation_budget or self.s.max_delegations

    def remember(self, clean: "CleanText") -> "CleanText":
        """Register a certified piece of content; the outbound guard lets it through based on this."""
        self.certified.add(sha256(clean.text))
        return clean

    def trace_note(self, **fields) -> None:
        """Structured facts a node body wants in its trace record; merged into the next trace_add."""
        self._trace_pending.update(fields)

    def trace_add(self, node: str, **fields) -> None:
        self.trace.append({"node": node, **self._trace_pending, **fields})
        self._trace_pending = {}

    def schedule_check(self, coro) -> None:
        """Attach a background check (coroutine) to the task; the coroutine writes its result back into the state itself."""
        self.pending_checks = [t for t in self.pending_checks if not t.done()]
        self.pending_checks.append(asyncio.ensure_future(coro))

    async def settle_checks(self) -> None:
        """Wait for all background checks to finish. Call before any decision that depends on tainted / stuck."""
        while pending := [t for t in self.pending_checks if not t.done()]:
            await asyncio.gather(*pending, return_exceptions=True)
        self.pending_checks.clear()

    def cancel_checks(self) -> None:
        for t in list(self.pending_checks):
            t.cancel()
        self.pending_checks.clear()
