"""Core orchestration: route -> snapshot -> run per mode -> audit. The core never touches the UI; it only emits events."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from pydantic_ai import Agent
from pydantic_ai.exceptions import UsageLimitExceeded

from .agents import (ExecutorOutput, ExecutorReport, ModelFactory, Review, Status, build_cloud_agent, build_executor,
                     build_planner, build_reviewer, compress_history, require_clean, restore_local, review_prompt,
                     stream_handler, trim_history, usage_limits)
from .audit import AuditLog, JsonlLog, sha256
from .complexity import RouteLLMScorer
from .config import RunMode, Settings
from . import project_doc
from .recon import workspace_recon
from .events import (ChatMessage, EventBus, Notice, ReportArrived, ReviewArrived, RouteDecided, SnapshotTaken,
                     StatsUpdated, TaskFinished)
from .judge import Judge, OllamaJudge
from .policy import Force, Route
from .privacy import CleanText, PrivacyGate, PrivacyVerdict
from .router import EntryRouter, Routing
from .sandbox import Sandbox
from .session import Session, TaskState
from .snapshot import SnapshotManager
from .web import WebClient

log = logging.getLogger(__name__)

VERIFY_QUESTION = ("Does the answer below complete the task correctly and completely, so that a stronger model does not "
                   "need to redo it?")

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
    backend: str   # ollama / deepseek / deepseek-plan+ollama
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
            command_log=JsonlLog(s.command_log_path), review_log=JsonlLog(s.review_log_path))
        if web is None:
            web = WebClient(s) if s.web_enabled else None
        self.session.web = web or None
        self.router = EntryRouter(s, judge, gate, self.scorer)
        stats = self.session.stats
        stats.on_change = lambda: self.bus.emit(StatsUpdated(stats.snapshot()))
        self.models.stats = stats
        if isinstance(judge, OllamaJudge):
            judge.usage_sink = stats.judge_usage
        self.executor = build_executor(self.models)

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
        changing the worker rebuilds it; the judge reads the config on every request; the DeepSeek agents are rebuilt
        per task. Returns what actually changed."""
        s, changed = self.s, {}
        if worker and worker != s.worker_model:
            s.worker_model = worker
            self.executor = build_executor(self.models)
            changed["WORKER_MODEL"] = worker
        if judge and judge != s.judge_model:
            s.judge_model = judge
            changed["JUDGE_MODEL"] = judge
        if cloud and cloud != s.deepseek_model:
            s.deepseek_model = cloud
            changed["DEEPSEEK_MODEL"] = cloud
        if plan and plan != s.deepseek_plan_model:
            s.deepseek_plan_model = plan
            changed["DEEPSEEK_PLAN_MODEL"] = plan
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

    async def run(self, task: str, material: str = "", force: Force = Force.NONE) -> TaskResult:
        text = f"{task}\n\n{material}".strip() if material else task
        st = TaskState(self.session, text, project_doc=project_doc.load(self.s.workspace))
        t0 = time.time()
        self.session.stats.task_started_at = t0
        self.bus.emit(ChatMessage("user", task))
        result: Optional[TaskResult] = None
        routing: Optional[Routing] = None
        try:
            routing = await self.router.route(task, text, force)
            d = routing.decision
            st.sensitive_input = routing.verdict.sensitive
            st.route = d.route.value
            self.bus.emit(RouteDecided(d.route.value, d.reasons, routing.signals_dict()))
            handler = {Route.LOCAL: self._local, Route.LOCAL_VERIFY: self._local_verify,
                       Route.CLOUD: self._cloud, Route.PLAN: self._plan}[d.route]
            result = await handler(st, routing)
            result.reasons = d.reasons + result.reasons
            result.signals = routing.signals_dict()
            return result
        finally:
            self.session.stats.task_started_at = None
            if result is None:
                st.cancel_checks()  # interrupted / failed: stop waiting for the background judge calls
            else:
                await st.settle_checks()
                if self.s.lessons_enabled and st.review_fixed:
                    await self._write_lesson(st)
            r = result or TaskResult("", routing.decision.route.value if routing else "cancelled", "none",
                                     ["Task was interrupted or failed"])
            r.outbound_count, r.tainted = st.outbound_count, st.tainted
            self.session.audit.task(text=text, route=r.route, reasons=r.reasons,
                                    signals=routing.signals_dict() if routing else {}, backend=r.backend,
                                    outbound_count=st.outbound_count, latency_s=time.time() - t0,
                                    tainted=st.tainted, mode=self.s.mode.value)
            self._record_progress(task, r, st, interrupted=result is None)
            if result is not None:
                self.bus.emit(TaskFinished(r.route, r.backend, r.output, st.outbound_count))

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

    async def _run_executor(self, st: TaskState, prompt: str) -> ExecutorOutput:
        st.tool_calls, st.stuck, st.recent_calls, st.tool_seq = 0, False, [], []
        if st.project_doc:
            prompt = f"[Project doc AGENT.md]\n{st.project_doc}\n\n{prompt}"
        history = trim_history(self.session.exec_history)  # deterministic trim first; compress with the local model if still too long
        history = await compress_history(self.models, history, self.s.history_compress_chars,
                                         timeout=self.s.compress_timeout_s)
        try:
            res = await asyncio.wait_for(
                self.executor.run(prompt, deps=st, message_history=history, usage_limits=usage_limits(self.s)),
                timeout=self.s.executor_run_timeout_s)
            out = res.output
            self.session.exec_history = res.all_messages()
        except UsageLimitExceeded as e:
            self.bus.emit(Notice("warn", f"Executor hit the usage limit: {e}"))
            out = ExecutorOutput(report=ExecutorReport(status=Status.PARTIAL,
                                                       issues=["Step limit reached; task not finished"]))
        except asyncio.TimeoutError:
            self.session.sandbox.kill_all()
            self.bus.emit(Notice("warn", f"Executor run exceeded {self.s.executor_run_timeout_s:.0f}s; this step was aborted"))
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

    async def _execute_reviewed(self, st: TaskState, prompt: str, task_text: Optional[str] = None,
                                acceptance: Optional[list[str]] = None) -> tuple[ExecutorOutput, Optional[Review]]:
        """After the executor claims done, the local reviewer verifies; on failure the problems go back to the executor
        to fix, up to verify_rounds rounds."""
        out = await self._run_executor(st, prompt)
        review: Optional[Review] = None
        rounds = self.s.verify_rounds
        for rnd in range(1, rounds + 1):
            if out.report.status is not Status.DONE:
                break
            review = await self._review(st, task_text or st.text, acceptance or [], out)
            if review is None:
                break
            last = review.passed or rnd == rounds
            st.last_review = review.model_dump()
            st.review_history.append(st.last_review)
            self.bus.emit(ReviewArrived(review.passed, review.problems, review.suggestions, rnd, last))
            if self.session.review_log:
                self.session.review_log.write({"task": sha256(st.text)[:12], "route": st.route, "round": rnd,
                                               "passed": review.passed, "problems": review.problems,
                                               "suggestions": review.suggestions})
            if review.passed:
                if st.review_failures:
                    st.review_fixed = True
                break
            st.review_failures += 1
            if last:
                break
            fix = (f"{prompt}\n\n[Review failed (round {rnd})]\nProblems:\n"
                   + "\n".join(f"- {x}" for x in review.problems) + "\nSuggestions:\n"
                   + "\n".join(f"- {x}" for x in review.suggestions)
                   + "\nFix each item, verify, then report.")
            out = await self._run_executor(st, fix)
        return out, review

    def _local_output(self, st: TaskState) -> str:
        parts = ["\n\n".join(st.answers)] if st.answers else []
        if st.artifacts:
            real = [restore_local(st, a["path"]) for a in st.artifacts]
            parts.append("Files produced:\n" + "\n".join(f"- {p}" for p in dict.fromkeys(real)))
        if st.last_review and not st.last_review.get("passed"):
            parts.append("⚠ Local review failed:\n" + "\n".join(f"- {x}" for x in st.last_review.get("problems", [])))
        return "\n\n".join(parts) or "(the executor gave no answer)"

    # ------------------------------------------------------------ Modes
    async def _local(self, st: TaskState, routing: Routing, reasons: Optional[list[str]] = None) -> TaskResult:
        snap = st.snapshot_id or self._snapshot(st)
        st.snapshot_id = snap
        await self._execute_reviewed(st, st.text)
        return TaskResult(self._local_output(st), Route.LOCAL.value, "ollama", reasons or [], snapshot_id=snap,
                          report=st.last_report, artifacts=st.artifacts)

    async def _local_verify(self, st: TaskState, routing: Routing) -> TaskResult:
        snap = self._snapshot(st)
        st.snapshot_id = snap
        out, review = await self._execute_reviewed(st, st.text)
        if review is not None and not review.passed:
            note = "Local review failed after multiple rounds; escalating to cloud"
            p = 0.0
        else:
            state = (f"Task:\n{st.text}\n\nExecutor report:\n{out.report.model_dump_json()}\n\nAnswer:\n{out.answer}"
                     + (f"\n\nLocal review: {json.dumps(st.last_review, ensure_ascii=False)}" if st.last_review else ""))
            try:
                p = await asyncio.to_thread(self.session.judge.noul, state, VERIFY_QUESTION)
            except Exception as e:
                return TaskResult(self._local_output(st), Route.LOCAL_VERIFY.value, "ollama",
                                  [f"Self-check failed; keeping the local result: {e}"], snapshot_id=snap,
                                  report=st.last_report, artifacts=st.artifacts)
            if p >= self.s.verify_threshold and out.report.status is Status.DONE:
                return TaskResult(self._local_output(st), Route.LOCAL_VERIFY.value, "ollama",
                                  [f"Local self-check passed p={p:.2f}"], snapshot_id=snap, report=st.last_report,
                                  artifacts=st.artifacts)
            note = f"Local self-check failed p={p:.2f}; escalating to cloud"
        self.bus.emit(Notice("info", note))
        needs_ws = routing.signals.needs_workspace if routing.signals else True
        if needs_ws or st.tainted or st.artifacts:
            r = await self._plan(st, routing, take_snapshot=False)
        else:
            st.answers.clear()
            r = await self._cloud(st, routing)
        r.route = Route.LOCAL_VERIFY.value
        r.reasons.insert(0, note)
        r.snapshot_id = r.snapshot_id or snap
        return r

    async def _cloud(self, st: TaskState, routing: Routing) -> TaskResult:
        planning = bool(routing.signals and routing.signals.task.choice == "planning")
        try:
            clean = st.remember(await asyncio.to_thread(st.gate.certify, st.text))  # check once more before going outbound
        except PermissionError as e:
            return await self._fallback_local(st, routing, f"Outbound check blocked; running locally instead: {e}")
        try:
            agent = build_cloud_agent(self.models, planning)
            res = await agent.run(require_clean(clean), deps=st, event_stream_handler=stream_handler("planner", self.bus))
        except Exception as e:  # includes OutboundBlockedError, network errors, missing API key
            log.exception("DeepSeek call failed")
            return await self._fallback_local(st, routing, f"DeepSeek unavailable, falling back to local: {e}")
        self.bus.emit(ChatMessage("planner", res.output))
        return TaskResult(res.output, Route.CLOUD.value, "deepseek")

    async def _plan(self, st: TaskState, routing: Routing, take_snapshot: bool = True) -> TaskResult:
        if not self.models.cloud_available:
            return await self._fallback_local(st, routing, "DEEPSEEK_API_KEY not set; plan mode runs fully local")
        snap = self._snapshot(st) if take_snapshot else None
        st.snapshot_id = st.snapshot_id or snap
        notes: list[str] = []
        recon = ""
        if self.s.recon_enabled:
            try:
                recon = await workspace_recon(self.session.sandbox, bool(st.project_doc))
            except Exception as e:
                log.exception("Recon failed")
                notes.append(f"Workspace recon failed; the planner starts blind: {type(e).__name__}")
        outbound = await self._outbound_task(st, routing.verdict, notes, recon)
        if outbound is None:
            r = await self._local(st, routing, notes + ["de-identification failed; running fully local"])
            r.snapshot_id = r.snapshot_id or snap
            return r
        st.report_for_cloud = True
        try:
            planner = build_planner(self.models, self._delegated_step)
            res = await planner.run(require_clean(outbound), deps=st, usage_limits=usage_limits(self.s),
                                    event_stream_handler=stream_handler("planner", self.bus))
        except Exception as e:
            log.exception("Plan mode failed")
            st.report_for_cloud = False
            if st.answers:  # the executor already did part of the work: keep the results, do not rerun
                notes.append(f"Planner failed midway ({e}); keeping the finished local results")
                return TaskResult(self._local_output(st), Route.PLAN.value, "deepseek-plan+ollama", notes,
                                  snapshot_id=snap, report=st.last_report, artifacts=st.artifacts)
            r = await self._local(st, routing, notes + [f"Planner unavailable; running fully local: {e}"])
            r.snapshot_id = r.snapshot_id or snap
            return r
        summary = restore_local(st, res.output)
        self.bus.emit(ChatMessage("planner", summary))
        output = summary + ("\n\n---\nLocal execution result:\n" + self._local_output(st)
                            if st.answers or st.artifacts else "")
        return TaskResult(output, Route.PLAN.value, "deepseek-plan+ollama", notes, snapshot_id=snap,
                          report=st.last_report, artifacts=st.artifacts)

    async def _delegated_step(self, st: TaskState, local_step: str,
                              acceptance: list[str]) -> tuple[ExecutorOutput, Optional[Review], str]:
        """One step delegated by the planner: execute -> local review against the acceptance criteria -> fix; on
        failure, produce a data-free diagnosis for the planner."""
        prompt = f"[Overall task]\n{st.text}\n\n[Current step delegated by the planner]\n{local_step}"
        if acceptance:
            prompt += "\n\n[Acceptance criteria]\n" + "\n".join(f"- {a}" for a in acceptance)
        # Take another snapshot before each delegation: the reviewer sees only this step's changes, so the diff does
        # not keep growing across a multi-step task
        st.snapshot_id = self._snapshot(st, label="step") or st.snapshot_id
        out, review = await self._execute_reviewed(st, prompt, task_text=local_step, acceptance=acceptance)
        failed = out.report.status is not Status.DONE or (review is not None and not review.passed)
        diagnosis = await self._diagnose(st, out, review) if failed else ""
        return out, review, diagnosis

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
                          capabilities=self.models.tracker("diagnosis", "local"))
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
                          capabilities=self.models.tracker("lesson", "local"))
            lesson = (await asyncio.wait_for(agent.run(LESSON_PROMPT + material), self.s.compress_timeout_s)).output
            lesson = lesson.strip().splitlines()[0].strip() if lesson.strip() else ""
            if lesson:
                project_doc.record_lesson(self.s.workspace, lesson)
                self.bus.emit(Notice("info", f"Lesson recorded to AGENT.md: {lesson}"))
        except Exception as e:
            log.warning("Lesson recording failed (%s)", type(e).__name__)

    async def _fallback_local(self, st: TaskState, routing: Routing, why: str) -> TaskResult:
        self.bus.emit(Notice("warn", why))
        return await self._local(st, routing, [why])

    async def _outbound_task(self, st: TaskState, verdict: PrivacyVerdict, notes: list[str],
                             recon: str = "") -> Optional[CleanText]:
        """The task description the planner sees: no private data -> original text (re-certified); rule entities only
        -> placeholders; contextually sensitive -> abstracted rewrite by a local model.
        The workspace AGENT.md and the recon overview are attached and take the same gate path as the task."""
        gate = st.gate
        source = st.text
        extras = ([f"[Project doc AGENT.md]\n{st.project_doc}"] if st.project_doc else []) + ([recon] if recon else [])
        if extras:
            source = "\n\n".join([st.text, *extras])
            verdict = await asyncio.to_thread(gate.check, source)  # the routing-stage verdict covered only the task text
        if not verdict.sensitive:
            try:
                clean = await asyncio.to_thread(gate.certify, source)
                notes.append("Task text passed the outbound check")
                return st.remember(clean)
            except PermissionError as e:
                notes.append(f"Original text failed the outbound check: {e}")
                verdict = await asyncio.to_thread(gate.check, source)
        if verdict.findings:  # try placeholders first; certify re-checks and decides whether contextual sensitivity remains
            redacted, mapping = gate.redact(source, verdict.findings)
            try:
                clean = await asyncio.to_thread(gate.certify, redacted)
                st.mapping.update(mapping)
                notes.append("Passed the outbound check after placeholder redaction")
                return st.remember(clean)
            except PermissionError as e:
                notes.append(f"Still failed after placeholder redaction: {e}")
        try:
            rewriter = Agent(self.models.compressor(), output_type=str, name="abstractor",
                             capabilities=self.models.tracker("abstraction", "local"))
            abstract = (await rewriter.run(ABSTRACT_PROMPT + source)).output.strip()
            clean = await asyncio.to_thread(gate.certify, abstract)
            notes.append("Local abstracted description passed the outbound check")
            return st.remember(clean)
        except PermissionError as e:
            notes.append(f"Abstracted description still failed: {e}")
        except Exception as e:
            log.exception("Abstraction rewrite failed")
            notes.append(f"Abstraction rewrite failed: {e}")
        return None
