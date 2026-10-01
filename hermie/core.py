"""Core orchestration: route -> snapshot -> run per mode -> audit. The core never touches the UI; it only emits events."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from pydantic_ai import Agent
from pydantic_ai.exceptions import UsageLimitExceeded

from .agents import (ExecutorOutput, ExecutorReport, ModelFactory, Review, Status, build_executor, build_reviewer,
                     compress_history, restore_local, review_prompt, trim_history, usage_limits)
from .audit import AuditLog, JsonlLog
from .complexity import RouteLLMScorer
from .config import RunMode, Settings
from . import project_doc
from .events import (ChatMessage, EventBus, ExecutorProgress, Notice, ReportArrived, SnapshotTaken,
                     StatsUpdated, TaskFinished)
from .judge import Judge, OllamaJudge
from .policy import Force, Route
from .privacy import CleanText, PrivacyGate, PrivacyVerdict
from .router import EntryRouter
from .sandbox import Sandbox
from .session import FlowState, Session, TaskState
from .snapshot import SnapshotManager
from .trajectory import task_record
from .graph import StepInput, build_step_graph, build_task_graph, run_step
from .mactools import ScreenCapture
from .web import WebClient

log = logging.getLogger(__name__)


def _mmss(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 60}:{s % 60:02d}"

DIAGNOSE_PROMPT = (
    "Below is an explanation of a failure (or a failed review) by the local executor. Rewrite it as a [Diagnosis] for the "
    "external planner, at most 3 sentences:\n"
    "state only the cause type (e.g. missing dependency, command not found, format mismatch, test failure, unclear "
    "requirements), what is missing, and a suggested alternative;\n"
    "remove all names, contact details, file contents, specific data and specific names inside paths. Output only the "
    "diagnosis, no explanation.\n\n"
)

LESSON_PROMPT = (
    "Below are the problems the local reviewer raised during a task, and the approach that finally fixed them. In one "
    "sentence (at most 25 words), write down a lesson that will be useful for this project going forward:\n"
    "which approach works, or which pitfall to avoid. Output only that sentence, no prefix.\n\n"
)

SKILL_PROMPT = (
    "Below is a step a local agent completed successfully (it passed review) and the tool calls it made. Write a short "
    "reusable playbook for doing this kind of step again, in this exact Markdown shape:\n\n"
    "# <title: what the playbook does, at most 10 words>\n\n## When to use\n<one or two sentences>\n\n## Steps\n"
    "1. <step>\n...\n\n## Verify\n- <how to check it worked, a command if there is one>\n\n"
    "Generalize: replace concrete file names, paths, values and names with a short description of what goes there. "
    "Never include personal data, secrets, customer or company names. Keep only commands that were shown to work. "
    "Output only the playbook.\n\n"
)


def split_playbook(text: str) -> Optional[tuple[str, str]]:
    """(title, body) of a distilled playbook, or None when the output is not one. A surrounding code fence is removed."""
    from .skills import HEADINGS
    t = (text or "").strip()
    fence = re.match(r"\A```[a-zA-Z]*\n(.*)\n```\Z", t, re.S)
    if fence:
        t = fence.group(1).strip()
    first, _, rest = t.partition("\n")
    if not first.startswith("# ") or not first[2:].strip() or not all(h in rest for h in HEADINGS):
        return None
    return first[2:].strip(), rest.strip()


ABSTRACT_PROMPT = (
    "Rewrite the task below as an [Abstracted task description] to ask an external expert for help drafting an execution "
    "plan. Requirements:\n"
    "1. Remove all names of people, organizations and places, dates, amounts, IDs, contact details and any internal or "
    "private detail;\n"
    "2. Keep only the task type, the goal, the structure of the input material (e.g. \"a spreadsheet with a number of "
    "customer records\") and the output requirements;\n"
    "3. Output only the rewritten description, no explanation.\n\nTask:\n"
)


@dataclass
class TaskResult:
    output: str
    route: str
    backend: str   # ollama / <cloud provider> / <cloud provider>-plan+ollama
    reasons: list[str] = field(default_factory=list)
    signals: dict = field(default_factory=dict)
    outbound_count: int = 0
    tainted: bool = False
    snapshot_id: Optional[str] = None
    report: Optional[dict] = None
    artifacts: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


class Hermie:
    def __init__(self, settings: Optional[Settings] = None, *, judge: Optional[Judge] = None,
                 analyzer=None, scorer: Optional[RouteLLMScorer] | bool = None,
                 models: Optional[ModelFactory] = None, bus: Optional[EventBus] = None,
                 web: Optional[WebClient] | bool = None):
        s = settings or Settings()
        s.ensure_dirs()
        judge = judge or OllamaJudge(s)
        gate = PrivacyGate(s, judge=judge, analyzer=analyzer)
        if scorer is None:
            scorer = RouteLLMScorer(s.routellm_checkpoint) if s.routellm_enabled else None
        self.scorer = scorer or None
        self.models = models or ModelFactory(s)
        self.session = Session(
            settings=s, judge=judge, gate=gate, sandbox=Sandbox(s),
            snapshots=SnapshotManager(s.workspace, s.snapshot_dir), bus=bus or EventBus(),
            audit=AuditLog(s.audit_log_path), outbound_log=JsonlLog(s.outbound_log_path),
            command_log=JsonlLog(s.command_log_path), review_log=JsonlLog(s.review_log_path),
            trajectory_log=JsonlLog(s.trajectory_log_path))
        if web is None:
            web = WebClient(s) if s.web_enabled else None
        from .memory import Embedder, LessonStore
        embedder = Embedder(s)  # shared: one "embeddings unavailable" state for lessons and skills
        self.startup_notes: list[str] = []   # problems found while starting; the UI shows them once it is up
        self.session.lessons = LessonStore(s.lessons_path, embedder) if s.lessons_enabled else None
        if s.skills_enabled:
            from .skills import SkillStore
            try:
                self.session.skills = SkillStore(s.skills_dir, embedder, s)
            except OSError as e:  # an unreadable skills directory must not stop Hermie from starting
                log.exception("Skill library unavailable")
                self.startup_notes.append(f"Skill library unavailable ({type(e).__name__}: {s.skills_dir}); "
                                          "skills are off for this session")
        # Post-task learning (lessons, skills) runs after the task is recorded and reported, as a background task, so
        # the result is not held up by local-model distillation and an interruption cannot lose the task record.
        self.learn_in_background = True
        self._learning: set[asyncio.Task] = set()
        self.session.web = web or None
        self.session.screen = ScreenCapture(s) if s.mac_tools else None
        self.router = EntryRouter(s, judge, gate, self.scorer)
        stats = self.session.stats
        stats.on_change = lambda: self.bus.emit(StatsUpdated(stats.snapshot()))
        self.models.stats = stats
        if isinstance(judge, OllamaJudge):
            judge.usage_sink = stats.judge_usage
        self.executor = build_executor(self.models)
        self.step_graph = build_step_graph(self)
        self.task_graph = build_task_graph(self)

    # ------------------------------------------------------------ Public interface
    @property
    def s(self) -> Settings:
        return self.session.settings

    @property
    def bus(self) -> EventBus:
        return self.session.bus

    def set_mode(self, mode: RunMode) -> None:
        self.s.mode = mode
        self.bus.emit(Notice("warn" if mode is not RunMode.DEFAULT else "info", f"Switched to {mode.label}"))

    def set_models(self, *, worker: Optional[str] = None, judge: Optional[str] = None,
                   cloud: Optional[str] = None, plan: Optional[str] = None) -> dict[str, str]:
        """Switch models; takes effect for the next task. The executor agent is built by model name at startup, so
        changing the worker rebuilds it; the judge reads the config on every request; the cloud agents are rebuilt
        per task. Returns what actually changed."""
        s, changed = self.s, {}
        if worker and worker != s.worker_model:
            s.worker_model = worker
            self.executor = build_executor(self.models)
            changed["WORKER_MODEL"] = worker
        if judge and judge != s.judge_model:
            s.judge_model = judge
            changed["JUDGE_MODEL"] = judge
        if cloud and cloud != s.cloud_model:
            s.cloud_model = cloud
            changed["CLOUD_MODEL"] = cloud
        if plan and plan != s.cloud_plan_model:
            s.cloud_plan_model = plan
            changed["CLOUD_PLAN_MODEL"] = plan
        return changed

    def list_local_models(self) -> list[str]:
        """Names of the models already pulled into Ollama."""
        import httpx
        r = httpx.get(f"{self.s.ollama_url}/api/tags", timeout=5)
        r.raise_for_status()
        return sorted(m["name"] for m in r.json().get("models", []))

    def warm_up(self) -> None:
        """Load spaCy and the RouteLLM weights up front (the first torch import takes tens of seconds) so the first
        request does not stall."""
        self.session.gate.warm_up()
        if self.scorer is not None:
            self.scorer.strong_win_rate("warm-up")

    def rollback(self, snapshot_id: Optional[str] = None) -> str:
        """Roll back to the given snapshot; with no id, the latest pre-task snapshot (plan-mode per-step snapshots
        do not count)."""
        snaps = self.session.snapshots.list()
        if snapshot_id:
            snap = next((x for x in snaps if x.id == snapshot_id), None)
        else:
            tasks = [x for x in snaps if x.label == "task"]
            snap = tasks[-1] if tasks else (snaps[-1] if snaps else None)
        if snap is None:
            raise LookupError("No snapshot available")
        self.session.sandbox.kill_all()
        self.session.snapshots.restore(snap)
        self.bus.emit(Notice("info", f"Rolled back to snapshot {snap.id} ({snap.kind})"))
        return snap.id

    def diff_since(self, snapshot_id: Optional[str]) -> str:
        snap = next((x for x in self.session.snapshots.list() if x.id == snapshot_id), None)
        return self.session.snapshots.diff(snap) if snap else ""

    def cancel_running(self) -> None:
        self.session.sandbox.kill_all()

    async def run(self, task: str, material: str = "", force: Force = Force.NONE,
                  read_roots: Sequence[Path] = ()) -> TaskResult:
        """read_roots: the files/directories the user attached, readable (never writable) by the executor for this
        task only (Sandbox.grant_read)."""
        text = f"{task}\n\n{material}".strip() if material else task
        # With lesson memory on, AGENT.md's "Lessons" section reaches the executor through recall_lessons instead of being
        # pasted in; with it off, the executor reads the section as before. The planner never gets it (_outbound_task).
        st = TaskState(self.session, text,
                       project_doc=project_doc.load(self.s.workspace, include_lessons=not self.s.lessons_enabled),
                       flow=FlowState(task=task, force=force))
        st.host = self
        await self._sync_lessons()
        t0 = time.time()
        self.session.stats.task_started_at = t0
        self.bus.emit(ChatMessage("user", task))
        result: Optional[TaskResult] = None
        try:
            with self.session.sandbox.grant_read(read_roots):
                result = await self.task_graph.run(state=st, deps=self)
            routing = st.flow.routing
            result.reasons = routing.decision.reasons + result.reasons
            result.signals = routing.signals_dict()
            return result
        finally:
            routing = st.flow.routing
            self.session.stats.task_started_at = None
            interrupted: Optional[BaseException] = None
            if result is None:
                st.cancel_checks()  # interrupted / failed: stop waiting for the background judge calls
            else:
                try:
                    await st.settle_checks()
                except asyncio.CancelledError as e:  # still record the task below, then re-raise
                    st.cancel_checks()
                    interrupted = e
            r = result or TaskResult("", routing.decision.route.value if routing else "cancelled", "none",
                                     ["Task was interrupted or failed"])
            r.outbound_count, r.tainted = st.outbound_count, st.tainted
            self.session.audit.task(text=text, route=r.route, reasons=r.reasons,
                                    signals=routing.signals_dict() if routing else {}, backend=r.backend,
                                    outbound_count=st.outbound_count, latency_s=time.time() - t0,
                                    tainted=st.tainted, mode=self.s.mode.value)
            if self.session.trajectory_log:
                self.session.trajectory_log.write(task_record(st, r, time.time() - t0, interrupted=result is None))
            self._record_progress(task, r, st, interrupted=result is None)
            if result is not None:
                status = "rejected" if st.flow.plan_rejected else str((r.report or {}).get("status") or "")
                self.bus.emit(TaskFinished(r.route, r.backend, r.output, st.outbound_count, status=status,
                                           elapsed_s=round(time.time() - t0, 1)))
            if interrupted is not None:
                raise interrupted
            if result is not None:
                if self.learn_in_background:
                    t = asyncio.ensure_future(self._learn(st))
                    self._learning.add(t)
                    t.add_done_callback(self._learning.discard)
                else:
                    await self._learn(st)

    async def _learn(self, st: TaskState) -> None:
        """Post-task learning, all local: a lesson after review-then-fix, lesson feedback and repeated-failure lessons,
        skill feedback and distillation. Runs after the task is recorded; each part logs and swallows its own errors."""
        if self.s.lessons_enabled and st.review_fixed:
            await self._write_lesson(st)
        if self.s.lessons_enabled and self.session.lessons is not None:
            await self._lessons_after_task(st)
        if self.s.skills_enabled and self.session.skills is not None:
            await self._skills_after_task(st)

    @property
    def learning(self) -> bool:
        """Post-task learning (lessons, skills) is still running in the background."""
        return bool(self._learning)

    async def learning_idle(self) -> None:
        """Wait for background post-task learning (the headless CLI calls this before exiting)."""
        if self._learning:
            await asyncio.gather(*list(self._learning), return_exceptions=True)

    async def cancel_learning(self) -> None:
        for t in list(self._learning):
            t.cancel()
        await self.learning_idle()

    def _record_progress(self, task: str, r: TaskResult, st: TaskState, interrupted: bool = False) -> None:
        """After a task ends (or is interrupted), append progress to the workspace AGENT.md (a deterministic write by
        the framework itself, no model involved). On interruption, record which plan steps completed and which files
        were produced, so that the next "continue" has something to go on."""
        try:
            rep = r.report or {}
            if interrupted:
                status = "Interrupted"
                route = Route(r.route).label if r.route in Route._value2member_map_ else "-"
                steps = [f"{'✓' if d else '✗'} {s}" for s, d in zip(st.plan_steps, st.plan_done)] or \
                        list((st.last_report or {}).get("steps_done", []))
                issues = ["Task was interrupted (Esc or error); the above is the progress before the interruption"]
                artifacts = [restore_local(st, a["path"]) for a in st.artifacts]
            else:
                status = Status(rep["status"]).label if rep.get("status") else ("Done" if r.output else "Not done")
                route = Route(r.route).label if r.route in Route._value2member_map_ else r.route
                steps, issues = list(rep.get("steps_done", [])), list(rep.get("issues", []))
                artifacts = [restore_local(st, a["path"]) for a in r.artifacts]
            entry = project_doc.format_entry(task, route, status, steps, artifacts, issues, summary=r.output)
            project_doc.record_progress(self.s.workspace, entry)
        except Exception as e:
            log.exception("Failed to write AGENT.md progress")
            self.bus.emit(Notice("warn", f"Could not update AGENT.md: {e}"))

    # ------------------------------------------------------------ Executor
    def _snapshot(self, st: TaskState, label: str = "task") -> Optional[str]:
        try:
            snap = self.session.snapshots.take(label=label, keep=self.s.snapshot_keep)  # label carries no task text
            self.bus.emit(SnapshotTaken(snap.id, snap.kind, label))
            return snap.id
        except Exception as e:
            log.exception("Snapshot failed")
            self.bus.emit(Notice("warn", f"Snapshot failed; rollback unavailable for this task: {e}"))
            return None

    async def _watch_executor(self, st: TaskState, run: asyncio.Future) -> Any:
        """Wait for an executor run, emitting ExecutorProgress every few seconds so the UI can show that the (slow)
        local model is still working. Stops it at executor_run_timeout_s; time spent waiting for the user's approval
        or input does not count. Warns once at 80% of the limit. Raises asyncio.TimeoutError on the limit."""
        limit = self.s.executor_run_timeout_s
        t0, waited0 = time.monotonic(), self.bus.user_wait_s()
        st.last_tool_at, warned = t0, False
        self.bus.emit(ExecutorProgress(0, 0, "", 0, limit, started=True))
        try:
            while True:
                elapsed = time.monotonic() - t0 - (self.bus.user_wait_s() - waited0)
                if elapsed >= limit:
                    raise asyncio.TimeoutError
                done, _ = await asyncio.wait({run}, timeout=min(self.s.progress_every_s, limit - elapsed))
                if done:
                    return run.result()
                elapsed = time.monotonic() - t0 - (self.bus.user_wait_s() - waited0)
                self.bus.emit(ExecutorProgress(round(elapsed), st.tool_calls, st.tool_seq[-1] if st.tool_seq else "",
                                               round(time.monotonic() - st.last_tool_at), limit))
                if not warned and elapsed >= 0.8 * limit:
                    warned = True
                    self.bus.emit(Notice("warn", f"The executor has been working for {_mmss(elapsed)}; it will be stopped "
                                                 f"at {_mmss(limit)} (EXECUTOR_RUN_TIMEOUT). Files it changed are kept."))
        finally:
            if not run.done():
                run.cancel()
                try:
                    await run
                except BaseException:
                    pass
            self.bus.emit(ExecutorProgress(0, st.tool_calls, "", 0, limit, done=True))

    def _timeout_notice(self, st: TaskState) -> str:
        last = (f"; last action {st.tool_seq[-1]} {_mmss(time.monotonic() - st.last_tool_at)} ago"
                if st.tool_seq else "; the local model had not finished its first reply")
        files = f" Files changed so far (kept): {', '.join(st.changed_paths[:8])}." if st.changed_paths else ""
        return (f"Stopped the executor: it hit the {_mmss(self.s.executor_run_timeout_s)} limit for one step "
                f"(EXECUTOR_RUN_TIMEOUT) after {st.tool_calls} tool calls{last}. Most of the time goes to the local model "
                f"({self.s.worker_model}) writing its replies.{files} Send \"continue\" to pick up from here, "
                f"or /rollback to undo.")

    async def _run_executor(self, st: TaskState, prompt: str) -> ExecutorOutput:
        st.tool_calls, st.stuck, st.recent_calls, st.tool_seq, st.changed_paths = 0, False, [], [], []
        if st.skills:
            store = self.session.skills
            prompt = ("[Skills: procedures that worked before - adapt, do not copy blindly]\n"
                      + "\n\n".join(f"### {sk.title}\n{store.body(sk)}" for sk in st.skills) + "\n\n" + prompt)
        if st.lessons:
            prompt = "[Lessons from earlier tasks]\n" + "\n".join(f"- {l.text}" for l in st.lessons) + "\n\n" + prompt
        if st.project_doc:
            prompt = f"[Project doc AGENT.md]\n{st.project_doc}\n\n{prompt}"
        history = trim_history(self.session.exec_history)  # deterministic trim first; compress with the local model if still too long
        history = await compress_history(self.models, history, self.s.history_compress_chars,
                                         timeout=self.s.compress_timeout_s)
        try:
            res = await self._watch_executor(st, asyncio.ensure_future(
                self.executor.run(prompt, deps=st, message_history=history, usage_limits=usage_limits(self.s))))
            out = res.output
            self.session.exec_history = res.all_messages()
        except UsageLimitExceeded as e:
            self.bus.emit(Notice("warn", f"Executor hit the usage limit: {e}"))
            out = ExecutorOutput(report=ExecutorReport(status=Status.PARTIAL,
                                                       issues=["Step limit reached; task not finished"]))
        except asyncio.TimeoutError:
            self.session.sandbox.kill_all()
            self.bus.emit(Notice("warn", self._timeout_notice(st)))
            out = ExecutorOutput(report=ExecutorReport(status=Status.PARTIAL,
                                                       issues=["Execution timed out; task not finished"]))
        await st.settle_checks()  # settle background taint / stuck checks: only then are tainted and the report event reliable
        st.last_report = out.report.model_dump(mode="json")
        st.artifacts.extend(a.model_dump() for a in out.report.artifacts)
        if out.answer:
            st.answers.append(out.answer)
            self.bus.emit(ChatMessage("executor", out.answer))
        if not st.report_for_cloud:
            self.bus.emit(ReportArrived(st.last_report))
        return out

    # ------------------------------------------------------------ Self-verification loop: execute -> local review -> fix
    async def _review(self, st: TaskState, task_text: str, acceptance: list[str],
                      out: ExecutorOutput) -> Optional[Review]:
        """The local reviewer looks at the workspace diff and command output and judges whether the task is really
        done. On error, skip (a failure in the quality loop does not affect the task)."""
        try:
            diff = await asyncio.to_thread(self.diff_since, st.snapshot_id) if st.snapshot_id else ""
            outputs = [c for c in st.recent_calls if c.startswith(("run_command", "read_file"))][-6:]
            prompt = review_prompt(task_text, acceptance, out, diff, outputs, self.s.review_diff_chars)
            res = await asyncio.wait_for(build_reviewer(self.models).run(prompt), self.s.review_timeout_s)
            return res.output
        except Exception as e:
            log.exception("Local review failed; skipping")
            self.bus.emit(Notice("warn", f"Reviewer error, skipping review: {type(e).__name__}"))
            return None

    def _local_output(self, st: TaskState) -> str:
        parts = ["\n\n".join(st.answers)] if st.answers else []
        if st.artifacts:
            real = [restore_local(st, a["path"]) for a in st.artifacts]
            parts.append("Files produced:\n" + "\n".join(f"- {p}" for p in dict.fromkeys(real)))
        if st.last_review and not st.last_review.get("passed"):
            parts.append("⚠ Local review failed:\n" + "\n".join(f"- {x}" for x in st.last_review.get("problems", [])))
        return "\n\n".join(parts) or "(the executor gave no answer)"

    async def _delegated_step(self, st: TaskState, local_step: str, acceptance: list[str],
                              plan_step: Optional[int] = None) -> tuple[ExecutorOutput, Optional[Review], str]:
        """One step delegated by the planner: execute -> local review against the acceptance criteria -> fix; on
        failure, produce a data-free diagnosis for the planner."""
        prompt = f"[Overall task]\n{st.text}\n\n[Current step delegated by the planner]\n{local_step}"
        if acceptance:
            prompt += "\n\n[Acceptance criteria]\n" + "\n".join(f"- {a}" for a in acceptance)
        # Take another snapshot before each delegation: the reviewer sees only this step's changes, so the diff does
        # not keep growing across a multi-step task
        st.snapshot_id = self._snapshot(st, label="step") or st.snapshot_id
        res = await run_step(self, st, StepInput(prompt, local_step, acceptance, diagnose=True))
        return res.out, res.review, res.diagnosis

    async def _diagnose(self, st: TaskState, out: ExecutorOutput, review: Optional[Review]) -> str:
        """Rewrite the executor's concrete failure explanation into a data-free diagnosis with a local model; if it
        does not certify, give nothing."""
        material = list(out.report.issues)
        if out.answer:
            material.append(out.answer[:2000])
        if review is not None and not review.passed:
            material += [f"Review problem: {x}" for x in review.problems]
        if not material:
            return ""
        try:
            agent = Agent(self.models.compressor(), output_type=str, name="diagnoser",
                          model_settings=self.models.local_settings(), capabilities=self.models.tracker("diagnosis", "local"))
            text = (await asyncio.wait_for(agent.run(DIAGNOSE_PROMPT + "\n".join(material)),
                                           self.s.compress_timeout_s)).output.strip()
            await asyncio.to_thread(st.gate.certify, text)
            return text
        except Exception as e:
            log.warning("Failure diagnosis not generated (%s)", type(e).__name__)
            return ""

    async def _write_lesson(self, st: TaskState) -> None:
        """A failed review was fixed successfully: have a local model summarize one lesson into the AGENT.md
        "Lessons" section. Local model only; failures are ignored."""
        try:
            problems = [p for rec in st.review_history for p in rec.get("problems", [])]
            fixed = (st.last_report or {}).get("steps_done", []) + (st.last_report or {}).get("verification", [])
            material = "Problems raised by the review:\n" + "\n".join(f"- {x}" for x in problems) + \
                       "\nFinal approach:\n" + "\n".join(f"- {x}" for x in fixed)
            agent = Agent(self.models.compressor(), output_type=str, name="lesson",
                          model_settings=self.models.local_settings(), capabilities=self.models.tracker("lesson", "local"))
            lesson = (await asyncio.wait_for(agent.run(LESSON_PROMPT + material), self.s.compress_timeout_s)).output
            lesson = lesson.strip().splitlines()[0].strip() if lesson.strip() else ""
            if lesson:
                project_doc.record_lesson(self.s.workspace, lesson)
                await self._store_lesson(st, lesson, "review_fixed")
                self.bus.emit(Notice("info", f"Lesson recorded to AGENT.md: {lesson}"))
        except Exception as e:
            log.warning("Lesson recording failed (%s)", type(e).__name__)

    async def _store_lesson(self, st: TaskState, text: str, source: str) -> None:
        store = self.session.lessons
        if store is None:
            return
        from .memory import workspace_id
        routing = st.flow.routing
        await asyncio.to_thread(store.add, text, workspace=workspace_id(self.s.workspace),
                                task_type=routing.signals.task.choice if routing and routing.signals else "",
                                tools=sorted(st.tools_used), source=source, key_text=st.flow.task)
        self._embedding_notice()

    def _embedding_notice(self) -> None:
        stores = [x for x in (self.session.lessons, self.session.skills) if x is not None]
        if any(getattr(x.embedder, "failed", False) for x in stores) and not self.session.lessons_notice_sent:
            self.session.lessons_notice_sent = True
            self.bus.emit(Notice("warn", f"Local embeddings unavailable ({self.s.lesson_embed_model} not reachable in "
                                         "Ollama): lessons are matched by word overlap and skills are paused"))

    async def _sync_lessons(self) -> None:
        """Import lessons written into AGENT.md by hand; stop using the ones the user deleted from it."""
        store = self.session.lessons
        if store is None or not self.s.lessons_enabled:
            return
        from .memory import workspace_id
        try:
            await asyncio.to_thread(store.sync_doc, workspace_id(self.s.workspace),
                                    project_doc.lessons(self.s.workspace), project_doc.MAX_LESSONS)
        except Exception:
            log.exception("Lesson sync with AGENT.md failed")

    async def _lessons_after_task(self, st: TaskState) -> None:
        """Feedback for the lessons injected in this task, and a lesson for every problem the reviewer raised twice or
        more without it being fixed. Local only; failures are logged and ignored."""
        store = self.session.lessons
        try:
            if st.lessons_used and st.review_history:
                helped = bool(st.review_history[0].get("passed"))
                await asyncio.to_thread(store.feedback, sorted(st.lessons_used), helped)
            for count, wording in st.problem_counts.values():
                if count >= 2:
                    lesson = f"Raised {count} times by the reviewer and not resolved: {wording}"
                    project_doc.record_lesson(self.s.workspace, lesson)
                    await self._store_lesson(st, lesson, "repeated_failure")
            self._embedding_notice()
        except Exception:
            log.exception("Lesson bookkeeping failed")

    async def _skills_after_task(self, st: TaskState) -> None:
        """Feedback for injected skills (retire the ones that keep not helping), then distill at most two reviewed
        episodes of this task into candidate playbooks. Local model only; tasks that touched sensitive data never
        distill; a playbook that fails the privacy check is dropped. Failures are logged and ignored."""
        store = self.session.skills
        try:
            if st.skills_used and st.review_history:
                helped = bool(st.review_history[0].get("passed"))
                for sk in await asyncio.to_thread(store.feedback, sorted(st.skills_used), helped):
                    self.bus.emit(Notice("info", f"Skill retired (it kept not helping): {sk.title}"))
            if st.exposed or not st.skill_episodes:
                return
            from .memory import workspace_id
            routing = st.flow.routing
            task_type = routing.signals.task.choice if routing and routing.signals else ""
            for ep in st.skill_episodes[:2]:
                material = (f"Step:\n{ep['step']}\n\nTool calls:\n" + "\n".join(ep["calls"][-30:])
                            + "\n\nReported steps:\n" + "\n".join(f"- {x}" for x in ep["steps"])
                            + "\nVerification:\n" + "\n".join(f"- {x}" for x in ep["verification"]))
                agent = Agent(self.models.compressor(), output_type=str, name="skill",
                              model_settings=self.models.local_settings(), capabilities=self.models.tracker("skill", "local"))
                out = (await asyncio.wait_for(agent.run(SKILL_PROMPT + material), self.s.compress_timeout_s)).output
                parsed = split_playbook(out)
                if parsed is None:
                    log.info("Skill distillation produced no playbook; dropped")
                    continue
                title, body = parsed
                verdict = await asyncio.to_thread(st.gate.check, f"{title}\n{body}")
                if verdict.sensitive:
                    log.info("Distilled skill dropped by the privacy check (%s)", verdict.reason)
                    continue
                sk, merged = await asyncio.to_thread(store.add_candidate, title, body,
                                                     workspace=workspace_id(self.s.workspace), task_type=task_type,
                                                     key_text=ep["step"])
                if merged and sk.status == "active" and sk.confirmations == 1:
                    self.bus.emit(Notice("info", f"New skill confirmed and active: {sk.title}"))
            self._embedding_notice()
        except Exception:
            log.exception("Skill bookkeeping failed")

    async def _certify_outbound(self, st: TaskState, source: str, notes: list[str],
                                verdict: Optional[PrivacyVerdict] = None) -> tuple[Optional[CleanText], str]:
        """The gate ladder for any local text bound for the planner: the original if it certifies; else Presidio
        placeholders (mapping merged into st.mapping, numbering continued); else a local abstracted rewrite; else
        nothing ("withheld"). Returns (remembered CleanText or None, how)."""
        gate = st.gate
        if verdict is None:
            verdict = await asyncio.to_thread(gate.check, source)
        if not verdict.sensitive:
            try:
                clean = await asyncio.to_thread(gate.certify, source)
                notes.append("Passed the outbound check")
                return st.remember(clean), "original"
            except PermissionError as e:
                notes.append(f"Original text failed the outbound check: {e}")
                verdict = await asyncio.to_thread(gate.check, source)
        if verdict.findings:  # try placeholders first; certify re-checks and decides whether contextual sensitivity remains
            redacted, mapping = gate.redact(source, verdict.findings, existing=st.mapping)
            try:
                clean = await asyncio.to_thread(gate.certify, redacted)
                st.mapping.update(mapping)
                notes.append("Passed the outbound check after placeholder redaction")
                return st.remember(clean), "redacted"
            except PermissionError as e:
                notes.append(f"Still failed after placeholder redaction: {e}")
        try:
            rewriter = Agent(self.models.compressor(), output_type=str, name="abstractor",
                             model_settings=self.models.local_settings(), capabilities=self.models.tracker("abstraction", "local"))
            abstract = (await rewriter.run(ABSTRACT_PROMPT + source)).output.strip()
            clean = await asyncio.to_thread(gate.certify, abstract)
            notes.append("Local abstracted description passed the outbound check")
            return st.remember(clean), "abstracted"
        except PermissionError as e:
            notes.append(f"Abstracted description still failed: {e}")
        except Exception as e:
            log.exception("Abstraction rewrite failed")
            notes.append(f"Abstraction rewrite failed: {e}")
        return None, "withheld"

    async def _outbound_task(self, st: TaskState, verdict: PrivacyVerdict, notes: list[str],
                             recon: str = "") -> Optional[CleanText]:
        """The task description the planner sees: no private data -> original text (re-certified); rule entities only
        -> placeholders; contextually sensitive -> abstracted rewrite by a local model.
        The workspace AGENT.md and the recon overview are attached and take the same gate path as the task."""
        source = st.text
        doc = project_doc.strip_lessons(st.project_doc).strip() if st.project_doc else ""  # lessons never go to the planner
        extras = ([f"[Project doc AGENT.md]\n{doc}"] if doc else []) + ([recon] if recon else [])
        if extras:
            source = "\n\n".join([st.text, *extras])
            verdict = None  # the routing-stage verdict covered only the task text
        clean, how = await self._certify_outbound(st, source, notes, verdict)
        if how == "original":
            notes[-1] = "Task text passed the outbound check"
        return clean
