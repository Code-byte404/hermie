"""Textual full-screen split-pane UI: makes the data flow obvious at a glance -- who runs each step, and what left this machine.

+ mode | outbound N, all passed the gate | executor | planner | workspace +
+- route/plan -+- chat -+- [execution log] outbound changes perf -+
+- input box (Enter to send, Ctrl+J for newline)                 -+
+- Esc interrupt · F2 switch mode · Ctrl+B/Ctrl+R collapse side panels +

The core never calls the UI directly: event bus -> CoreEvent message -> render.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Optional

from rich.markup import escape
from rich.syntax import Syntax
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import (Button, Input, Label, Markdown, OptionList, RichLog, Select, Static, Switch, TabbedContent,
                             TabPane, TextArea)
from textual.widgets.option_list import Option

from ..attachments import Material, find_paths, human_size, load_material
from ..config import RunMode, Settings, update_env
from ..perf import PerfSample, PerfSampler, render_graph
from .commands import filter_commands, find_command, help_markdown, is_command
from ..voice import Recorder, Speaker, Transcriber, VoiceUnavailable, is_blank_transcript, phrase_for
from ..events import (Approval, ApprovalRequest, ChatMessage, ChoiceRequest, ClarifyAnswer, ClarifyRequest, CommandFinished,
                      CommandStarted, Event, ExecutorProgress, InputRequest, Notice, OutboundBlocked, OutboundSent,
                      PlanDecision, PlanProposed, PlanReviewRequest, PlanUpdated, ReportArrived, ReviewArrived,
                      RouteDecided, SnapshotTaken, StatsUpdated, Tainted, TaskFinished)
from ..policy import Force, Route

log = logging.getLogger(__name__)



def _k(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _mmss(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}" if m < 60 else f"{m // 60}:{m % 60:02d}:{s:02d}"


def _gb(n: int) -> str:
    return f"{n / (1 << 30):.1f}"


def _bar(pct: float, width: int = 24) -> str:
    n = max(0, min(width, round(pct / 100 * width)))
    return "▇" * n + "░" * (width - n)


class PerfPanel(Vertical):
    """The "Perf" tab: live CPU / GPU usage bars + a two-minute history graph (perf.render_graph), one line for memory. Data comes from perf.PerfSampler."""

    GRAPH_ROWS = 4

    def compose(self) -> ComposeResult:
        yield Static("CPU   waiting for sample...", id="perf-cpu", classes="perf-label")
        yield Static("", id="perf-cpu-graph", classes="perf-graph cpu")
        yield Static("GPU   waiting for sample...", id="perf-gpu", classes="perf-label")
        yield Static("", id="perf-gpu-graph", classes="perf-graph gpu")
        yield Static("MEM   waiting for sample...", id="perf-mem", classes="perf-label")

    def show(self, s: PerfSample, cpu_hist, gpu_hist) -> None:
        self.query_one("#perf-cpu", Static).update(Text.assemble(("CPU   ", "bold"), (_bar(s.cpu), "green"),
                                                                f" {s.cpu:3.0f}%"))
        if s.gpu is None:
            self.query_one("#perf-gpu", Static).update(Text.assemble(("GPU   ", "bold"), ("unavailable (ioreg returned no utilization)", "dim")))
        else:
            gpu_mem = f"  · memory in use {_gb(s.gpu_mem)} GB" if s.gpu_mem else ""
            self.query_one("#perf-gpu", Static).update(Text.assemble(("GPU   ", "bold"), (_bar(s.gpu), "yellow"),
                                                                    f" {s.gpu:3.0f}%", (gpu_mem, "dim")))
        pct = s.mem_used / s.mem_total * 100 if s.mem_total else 0.0
        self.query_one("#perf-mem", Static).update(Text.assemble(("MEM   ", "bold"), (_bar(pct), "cyan"),
                                                                f" {pct:3.0f}%  {_gb(s.mem_used)} / {_gb(s.mem_total)} GB"))
        for wid, hist in (("#perf-cpu-graph", cpu_hist), ("#perf-gpu-graph", gpu_hist)):
            g = self.query_one(wid, Static)
            width = max(10, g.content_size.width or (self.content_size.width - 0))
            g.update("\n".join(render_graph(hist, width, self.GRAPH_ROWS)))


class CoreEvent(Message):
    def __init__(self, event: Event):
        super().__init__()
        self.event = event


# How each report status is shown when a task finishes: (icon, word, status-line style)
_FINISH = {"done": ("✔", "Done", "done"), "partial": ("◐", "Partially done", "partial"),
           "failed": ("✖", "Failed", "failed"), "needs_clarification": ("❓", "Needs your clarification", "partial"),
           "rejected": ("⊘", "Plan rejected, nothing executed", "stopped")}
# What the app is doing while a request from this agent role is in flight
_ROLE_DOING = {"executor": "🔒 Local executor is writing its reply", "reviewer": "🔍 Local reviewer is checking the work",
               "compressor": "🗜 Local model is compressing history", "planner": "☁ Cloud planner is writing its reply",
               "cloud": "☁ Cloud model is answering", "diagnosis": "🩺 Local model is diagnosing a failed step",
               "abstraction": "🔒 Local model is abstracting the task before it goes to the cloud",
               "lesson": "📚 Local model is writing a lesson", "skill": "📚 Local model is distilling a skill",
               "designer": "☁ Cloud planner is designing the plan",
               "cloud_executor": "☁ Cloud executor is driving the local tools"}


class PromptInput(TextArea):
    """Multi-line input box: Enter sends, Ctrl+J inserts a newline."""

    class Submitted(Message):
        def __init__(self, value: str):
            super().__init__()
            self.value = value

    async def _on_key(self, event) -> None:
        app = self.app
        if getattr(app, "popup_visible", False) and event.key in ("up", "down", "tab", "escape", "enter"):
            if app.popup_key(event.key):   # the completion popup consumed this key
                event.prevent_default()
                event.stop()
                return
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            value = self.text.strip()
            if value:
                self.post_message(self.Submitted(value))
                self.clear()
            return
        if event.key == "ctrl+j":
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return
        await super()._on_key(event)

    async def _on_paste(self, event: events.Paste) -> None:
        # A dragged file arrives as a paste of its path; keep it apart from a word typed right before it.
        event.prevent_default()   # replaces TextArea._on_paste, which would insert the text a second time
        text = event.text
        if text.startswith(("/", "~", "'", '"')):
            before = self.document.get_text_range((0, 0), self.cursor_location)
            if before and not before[-1].isspace():
                text = " " + text
        if result := self._replace_via_keyboard(text, *self.selection):
            self.move_cursor(result.end_location)
        self.focus()
        event.stop()   # handled; don't let it bubble to HermieApp.on_paste


class ApprovalScreen(ModalScreen[Approval]):
    BINDINGS = [Binding("y", "choose('allow')", "Allow"), Binding("n,escape", "choose('deny')", "Deny"),
                Binding("a", "choose('allow_session')", "Allow for this session")]

    def __init__(self, req: ApprovalRequest):
        super().__init__()
        self.req = req

    def compose(self) -> ComposeResult:
        with Vertical(id="approval"):
            yield Label(f"⚠ Approval needed: {self.req.reason} (risk: {self.req.risk})", id="approval-title")
            yield Static(Syntax(self.req.summary, "bash", word_wrap=True), id="approval-cmd")
            with Horizontal(id="approval-buttons"):
                yield Button("Allow (y)", id="allow", variant="success")
                yield Button("Deny (n)", id="deny", variant="error")
                yield Button("Allow for this session (a)", id="allow_session", variant="warning")

    @on(Button.Pressed)
    def pressed(self, ev: Button.Pressed) -> None:
        self.dismiss(Approval(ev.button.id))

    def action_choose(self, value: str) -> None:
        self.dismiss(Approval(value))


class ChoiceScreen(ModalScreen[Optional[str]]):
    """A connector needs a pick (an app, an Apple Ads organization): Enter takes the highlighted option, Esc cancels."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, req: ChoiceRequest):
        super().__init__()
        self.req = req

    def compose(self) -> ComposeResult:
        with Vertical(id="choice"):
            yield Label(Text(f"? {self.req.prompt}"), id="choice-title")
            yield OptionList(*[Option(Text(o), id=str(i)) for i, o in enumerate(self.req.options)], id="choice-list")
            yield Label("Enter: choose · Esc: cancel", id="choice-hint")

    def on_mount(self) -> None:
        self.query_one("#choice-list", OptionList).focus()

    @on(OptionList.OptionSelected, "#choice-list")
    def selected(self, ev: OptionList.OptionSelected) -> None:
        self.dismiss(self.req.options[int(ev.option.id)])

    def action_cancel(self) -> None:
        self.dismiss(None)


class InputScreen(ModalScreen[Optional[str]]):
    """A running command is waiting for input: Enter sends the typed line (empty = accept the default),
    Esc stops the command."""

    BINDINGS = [Binding("escape", "stop", "Stop command")]

    def __init__(self, req: InputRequest):
        super().__init__()
        self.req = req

    def compose(self) -> ComposeResult:
        with Vertical(id="input-request"):
            yield Label("⌨ The command is waiting for your input", id="input-title")
            yield Static(Syntax(self.req.command, "bash", word_wrap=True), id="input-cmd")
            yield Static(Text.from_ansi(self.req.output.rstrip()), id="input-output")
            yield Input(placeholder="Type the answer and press Enter (empty = default)", id="input-reply")
            with Horizontal(id="input-buttons"):
                yield Button("Send (Enter)", id="send", variant="success")
                yield Button("Stop command (Esc)", id="stop", variant="error")

    def on_mount(self) -> None:
        self.query_one("#input-reply", Input).focus()

    @on(Input.Submitted, "#input-reply")
    def submitted(self, ev: Input.Submitted) -> None:
        self.dismiss(ev.value)

    @on(Button.Pressed)
    def pressed(self, ev: Button.Pressed) -> None:
        if ev.button.id == "send":
            self.dismiss(self.query_one("#input-reply", Input).value)
        else:
            self.dismiss(None)

    def action_stop(self) -> None:
        self.dismiss(None)


class ClarifyScreen(ModalScreen[Optional[list[ClarifyAnswer]]]):
    """The planner's questions, one per page: Enter takes the highlighted option (the recommended one is first and
    highlighted), "Other..." opens a text box (Esc there returns to the options), Esc skips the question, Ctrl+X
    stops the task. With timeout_s (AUTO mode) the recommended options are taken for the remaining questions when the
    countdown ends."""

    # ctrl+x needs priority: the "Other..." Input binds ctrl+x to cut and the focused widget would win
    BINDINGS = [Binding("escape", "skip", "Skip question"), Binding("ctrl+x", "stop", "Stop the task", priority=True)]
    OTHER = "Other..."

    def __init__(self, req: ClarifyRequest):
        super().__init__()
        self.req = req
        self.answers: list[ClarifyAnswer] = []
        self.left = req.timeout_s
        self._timer = None
        self._answered = False   # not _closed: that name is MessagePump's own flag

    def compose(self) -> ComposeResult:
        with Vertical(id="clarify"):
            yield Label("", id="clarify-title")
            yield Static("", id="clarify-question")
            yield OptionList(id="clarify-options")
            yield Input(placeholder="Type your answer and press Enter", id="clarify-other")
            yield Static("Enter choose · ↑↓ move · Esc skip (planner decides) · Ctrl+X stop the task", id="clarify-help")

    def on_mount(self) -> None:
        self._page()
        if self.left:
            self._timer = self.set_interval(1.0, self._tick)

    def _page(self) -> None:
        i = len(self.answers)
        q = self.req.questions[i]
        self._title()
        self.query_one("#clarify-question", Static).update(
            escape(q.question) + (f"\n[dim]{escape(q.why)}[/dim]" if q.why else ""))
        opts = self.query_one("#clarify-options", OptionList)
        opts.clear_options()
        opts.add_options([Option(escape(o) + ("  (recommended)" if k == 0 else "")) for k, o in enumerate(q.options)]
                         + [Option(self.OTHER)])
        opts.highlighted = 0
        other = self.query_one("#clarify-other", Input)
        other.value, other.display = "", False
        opts.focus()

    def _title(self) -> None:
        i = len(self.answers)
        countdown = f" · recommended answers in {int(self.left)}s" if self.left else ""
        self.query_one("#clarify-title", Label).update(
            f"? Planner question {i + 1}/{len(self.req.questions)} · round {self.req.round}" + countdown)

    def _stop_countdown(self) -> None:
        """The user is present (typing an answer): no automatic answers any more."""
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        if self.left:
            self.left = None
            self._title()

    def _close(self, result: Optional[list[ClarifyAnswer]]) -> None:
        if self._answered:   # a countdown tick and a key press can race; dismiss pops the screen unconditionally
            return
        self._answered = True
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        self.dismiss(result)

    def _tick(self) -> None:
        if self._answered or not self.left:
            return
        self.left -= 1
        if self.left <= 0:
            while len(self.answers) < len(self.req.questions):
                self.answers.append(ClarifyAnswer(option=0))
            self._close(self.answers)
            return
        self._title()

    def _record(self, answer: ClarifyAnswer) -> None:
        if self._answered:
            return
        self.answers.append(answer)
        if len(self.answers) == len(self.req.questions):
            self._close(self.answers)
        else:
            self._page()

    @on(OptionList.OptionSelected, "#clarify-options")
    def chosen(self, ev: OptionList.OptionSelected) -> None:
        q = self.req.questions[len(self.answers)]
        if ev.option_index == len(q.options):
            self._stop_countdown()
            other = self.query_one("#clarify-other", Input)
            other.display = True
            other.focus()
            return
        self._record(ClarifyAnswer(option=ev.option_index))

    @on(Input.Submitted, "#clarify-other")
    def typed(self, ev: Input.Submitted) -> None:
        self._record(ClarifyAnswer(text=ev.value) if ev.value.strip() else ClarifyAnswer())

    def action_skip(self) -> None:
        other = self.query_one("#clarify-other", Input)
        if other.display:   # Esc while typing an answer: back to the options, the question stays open
            other.value, other.display = "", False
            self.query_one("#clarify-options", OptionList).focus()
            return
        self._record(ClarifyAnswer())

    def action_stop(self) -> None:
        self._close(None)


class PlanScreen(ModalScreen[PlanDecision]):
    """The plan to approve: Enter approves, c opens a change request (Ctrl+S sends), x or Esc rejects."""

    BINDINGS = [Binding("enter", "approve", "Approve", priority=True), Binding("c", "change", "Request changes"),
                Binding("x,escape", "reject", "Reject"), Binding("ctrl+s", "send", "Send changes", priority=True)]

    def __init__(self, req: PlanReviewRequest):
        super().__init__()
        self.req = req

    def compose(self) -> ComposeResult:
        title = "📋 Plan for your approval" if self.req.revision == 1 else f"📋 Revised plan (rev {self.req.revision})"
        ch = self.req.changed or {}
        if any(ch.get(k) for k in ("added", "changed", "removed")):
            nums = lambda xs: ", ".join(str(x) for x in xs) if xs else "-"
            title += (f" · added {nums(ch.get('added'))} · changed {nums(ch.get('changed'))}"
                      f" · removed {len(ch.get('removed') or [])}")
        with Vertical(id="plan-review"):
            yield Label(escape(title), id="plan-review-title")
            with VerticalScroll(id="plan-review-body"):   # Markdown itself is not focusable; the scroll is (arrows scroll)
                yield Markdown(self.req.markdown.replace("<!-- hermie-plan -->\n", ""))
            yield TextArea(id="plan-review-feedback")
            help_ = "Enter approve · x/Esc reject" + ("" if self.req.final else " · c request changes (Ctrl+S sends)")
            yield Static(help_, id="plan-review-help")

    def on_mount(self) -> None:
        self.query_one("#plan-review-body", VerticalScroll).focus()   # the feedback box starts hidden (app.tcss)

    def _editing(self) -> bool:
        return self.query_one("#plan-review-feedback", TextArea).display

    def action_approve(self) -> None:
        if self._editing():   # Enter inside the change request is a newline
            self.query_one("#plan-review-feedback", TextArea).insert("\n")
            return
        self.dismiss(PlanDecision("approve"))

    def action_change(self) -> None:
        if self.req.final or self._editing():
            return
        box = self.query_one("#plan-review-feedback", TextArea)
        box.display = True
        box.focus()

    def action_send(self) -> None:
        if self._editing():
            text = self.query_one("#plan-review-feedback", TextArea).text.strip()
            self.dismiss(PlanDecision("revise", text) if text else PlanDecision("approve"))

    def action_reject(self) -> None:
        if self._editing():   # Esc closes the change request box first
            self.query_one("#plan-review-feedback", TextArea).display = False
            self.query_one("#plan-review-body", VerticalScroll).focus()
            return
        self.dismiss(PlanDecision("reject"))


_KEY_RE = re.compile(r"^(f([3-9]|1[0-2])|ctrl\+[a-z])$")
_RESERVED_KEYS = {"ctrl+b", "ctrl+r", "ctrl+q", "ctrl+j", "ctrl+c", "f6", "f2"}


def valid_record_key(key: str) -> bool:
    return bool(_KEY_RE.match(key)) and key not in _RESERVED_KEYS


class ModelScreen(ModalScreen[Optional[dict]]):
    """Model config dialog: the two local models use a dropdown of installed Ollama models, the two cloud models are typed in. Save returns {worker, judge, cloud, plan}."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, s: Settings, local_models: list[str]):
        super().__init__()
        self.s = s
        self.local_models = sorted(set(local_models) | {s.worker_model, s.judge_model})

    def compose(self) -> ComposeResult:
        opts = [(m, m) for m in self.local_models]
        with Vertical(id="config"):
            yield Label("Model configuration", classes="config-title")
            yield Label("Local executor (Ollama)")
            yield Select(opts, value=self.s.worker_model, allow_blank=False, id="cfg-worker")
            yield Label("Local judge model (Ollama)")
            yield Select(opts, value=self.s.judge_model, allow_blank=False, id="cfg-judge")
            yield Label(f"Cloud direct model ({self.s.cloud_label})")
            yield Input(self.s.cloud_model, id="cfg-cloud")
            yield Label(f"Cloud planner model ({self.s.cloud_label})")
            yield Input(self.s.cloud_plan_model, id="cfg-plan")
            yield Label("", id="config-error")
            with Horizontal(classes="config-buttons"):
                yield Button("Save (Enter)", id="save", variant="primary")
                yield Button("Cancel (Esc)", id="cancel")

    @on(Button.Pressed, "#save")
    @on(Input.Submitted)
    def save(self, _ev=None) -> None:
        cloud = self.query_one("#cfg-cloud", Input).value.strip()
        plan = self.query_one("#cfg-plan", Input).value.strip()
        if not cloud or not plan:
            self.query_one("#config-error", Label).update("Cloud model name cannot be empty")
            return
        self.dismiss({"worker": self.query_one("#cfg-worker", Select).value,
                      "judge": self.query_one("#cfg-judge", Select).value, "cloud": cloud, "plan": plan})

    @on(Button.Pressed, "#cancel")
    def action_cancel(self, _ev=None) -> None:
        self.dismiss(None)


class VoiceScreen(ModalScreen[Optional[dict]]):
    """Voice config dialog: speech toggle, voice, rate, record key, with a test button. Save returns {enabled, voice, rate, key}."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, s: Settings, speaker):
        super().__init__()
        self.s, self.speaker = s, speaker

    def compose(self) -> ComposeResult:
        voices = self.speaker.list_voices()
        names = [n for n, _ in voices]
        current = self.speaker.voice or (names[0] if names else "")
        opts = [(f"{n} ({lang})", n) for n, lang in voices] or [(current or "System default", current)]
        if current and current not in names:
            opts.insert(0, (current, current))
        with Vertical(id="config"):
            yield Label("Voice configuration", classes="config-title")
            with Horizontal(classes="config-row"):
                yield Label("Speech output  ")
                yield Switch(value=self.speaker.enabled, id="cfg-enabled")
            yield Label("Voice")
            yield Select(opts, value=current, allow_blank=False, id="cfg-voice")
            yield Label("Rate (0 = system default)")
            yield Input(str(int(self.s.voice_rate or 0)), id="cfg-rate", type="integer")
            yield Label("Record key (F3-F12 or ctrl+letter; press once to start, again to stop)")
            yield Input(self.s.voice_key, id="cfg-key")
            yield Label("", id="config-error")
            with Horizontal(classes="config-buttons"):
                yield Button("Save (Enter)", id="save", variant="primary")
                yield Button("Test", id="preview")
                yield Button("Cancel (Esc)", id="cancel")

    def _values(self) -> Optional[dict]:
        key = self.query_one("#cfg-key", Input).value.strip().lower()
        rate = self.query_one("#cfg-rate", Input).value.strip() or "0"
        err = self.query_one("#config-error", Label)
        if not valid_record_key(key):
            err.update("The record key must be one of F3-F12 or ctrl+letter (ctrl+b/r/q/j/c and F2/F6 are taken)")
            return None
        if not rate.isdigit():
            err.update("Rate must be an integer")
            return None
        return {"enabled": self.query_one("#cfg-enabled", Switch).value,
                "voice": self.query_one("#cfg-voice", Select).value, "rate": int(rate), "key": key}

    @on(Button.Pressed, "#save")
    @on(Input.Submitted)
    def save(self, _ev=None) -> None:
        if (v := self._values()) is not None:
            self.dismiss(v)

    @on(Button.Pressed, "#preview")
    def preview(self) -> None:
        rate = self.query_one("#cfg-rate", Input).value.strip()
        self.speaker.preview("Hello, this is a test of the speech output.", self.query_one("#cfg-voice", Select).value,
                             int(rate) if rate.isdigit() else 0)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self, _ev=None) -> None:
        self.dismiss(None)


class HermieApp(App):
    TITLE = "Hermie"
    CSS_PATH = "app.tcss"
    BINDINGS = [
        Binding("escape", "interrupt", "Interrupt"),
        Binding("f2", "cycle_mode", "Switch mode"),
        Binding("f5", "toggle_record", "Record", priority=True),
        Binding("f6", "toggle_voice", "Speech", priority=True),   # the input box binds F6 to select-line itself; override it here
        Binding("ctrl+b", "toggle('#left')", "Left pane"),
        Binding("ctrl+r", "toggle('#right')", "Right pane"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self, settings: Optional[Settings] = None, agent=None, *, recorder=None, transcriber=None,
                 speaker=None, perf=None):
        super().__init__()
        self.settings = settings or (agent.s if agent else Settings())
        self._agent = agent
        s = self.settings
        self.perf = perf or PerfSampler()
        self.recorder = recorder or Recorder(s.voice_max_seconds)
        self.transcriber = transcriber or Transcriber(s.whisper_model)
        self.speaker = speaker or Speaker(s)
        self._transcribing = False
        self._rec_timer = None
        self.transcript: list[tuple[str, str]] = []
        self._stream_md: Optional[Markdown] = None
        self._stream = None
        self._stream_buf = ""
        self._last_snapshot: Optional[str] = None
        self._busy = False
        self._pending_attachments: list[str] = []
        self._voice_warned: set[str] = set()   # "voice unavailable" messages already shown in the chat
        # What the app is doing right now, for the status line (see _state): one source of truth for "is it still
        # running, waiting for me, or finished?"
        self._routed = False                                        # the current task got past routing
        self._running_cmd: Optional[tuple[str, str, float]] = None  # (tool, summary, monotonic start)
        self._waiting_user: Optional[tuple[str, float]] = None      # (what is asked, monotonic start)
        self._step: Optional[ExecutorProgress] = None               # latest heartbeat of a running executor step
        self._last_result: Optional[tuple[str, str]] = None         # (status line, style) of the last finished task
        self._replies_seen = 0
        self._was_learning = False

    # ------------------------------------------------------------ layout
    def compose(self) -> ComposeResult:
        yield Static(id="topbar")
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield Static("Route: —", id="route")
                yield Static("", id="plan")
            yield VerticalScroll(id="chat")
            with TabbedContent(id="right"):
                with TabPane("Log", id="tab-log"):
                    yield RichLog(id="log", max_lines=2000, wrap=True, markup=True)
                with TabPane("Outbound", id="tab-outbound"):
                    yield RichLog(id="outbound", max_lines=2000, wrap=True, markup=True)
                with TabPane("Changes", id="tab-diff"):
                    yield RichLog(id="diff", max_lines=5000, wrap=False)
                with TabPane("Perf", id="tab-perf"):
                    yield PerfPanel(id="perf")
        yield OptionList(id="cmd-popup")
        yield Static("", id="status")
        yield Static("", id="attachments")
        yield PromptInput(id="input", placeholder="› Type a task, drop files here (/help for commands)", soft_wrap=True)
        yield Static(self._hints_text(), id="hints")

    def _hints_text(self) -> str:
        return (f"Enter send · Ctrl+J newline · Esc interrupt · F2 switch mode · {self.settings.voice_key.upper()} record · "
                "F6 speech · Ctrl+B/R collapse panes · /help")

    @property
    def agent(self):
        if self._agent is None:
            from ..core import Hermie
            self._agent = Hermie(self.settings)
        return self._agent

    def on_mount(self) -> None:
        self.agent.bus.subscribe(lambda ev: self.post_message(CoreEvent(ev)))
        self.agent.bus.approver = self._approve
        self.agent.bus.input_provider = self._provide_input
        self.agent.bus.chooser = self._choose
        self.agent.bus.clarifier = self._clarify
        self.agent.bus.plan_reviewer = self._review_plan
        if self.settings.voice_key != "f5":
            self._bind_record_key(self.settings.voice_key, "f5")
        self._refresh_topbar()
        self._perf_timer = self.set_interval(1.0, self._tick_perf)
        self._status_timer = self.set_interval(1.0, self._tick_status)
        self._refresh_status()
        self.query_one("#input").focus()
        if self.settings.mode is RunMode.NO_SANDBOX:
            self._notice("error", "No-sandbox mode: the executor can access the network and read/write the whole disk. The privacy gate is still on.")
        if not self.settings.cloud_api_key:
            self._notice("warn", "CLOUD_API_KEY not set: plan mode and cloud direct will fall back to local execution.")
        for note in getattr(self.agent, "startup_notes", []):
            self._notice("warn", note)
        self._warm_up()

    @work(thread=True, exclusive=True, group="warmup")
    def _warm_up(self) -> None:
        self.call_from_thread(self._notice, "info", "Loading privacy detection models...")
        try:
            self.agent.warm_up()
            from ..project_doc import find_doc
            doc = find_doc(self.settings.workspace)
            hint = f"loaded {doc.name}" if doc else "no AGENT.md; it will be created when a task finishes"
            self.call_from_thread(self._notice, "info", f"Ready. Workspace: {self.settings.workspace} ({hint})")
        except Exception as e:
            self.call_from_thread(self._notice, "error", f"Failed to load privacy detection models (every request will be treated as sensitive): {e}")

    # ------------------------------------------------------------ topbar
    def _refresh_topbar(self) -> None:
        s = self.settings
        bar = self.query_one("#topbar", Static)
        for m in RunMode:
            bar.remove_class(f"mode-{m.value}")
        bar.add_class(f"mode-{s.mode.value}")
        st = self.agent.session.stats.snapshot()
        n = self.agent.session.outbound_total
        task_t = f" │ task {_mmss(st['task_elapsed_s'])}" if st["task_elapsed_s"] is not None else ""
        voice = "🔊" if self.speaker.enabled else "🔇"
        line1 = (f" {s.mode.label} │ {voice} │ outbound {n}, all passed the gate{task_t} │ "
                 f"session {_mmss(st['session_elapsed_s'])} │ {s.workspace}")
        c, l = st["cloud"], st["local"]
        runs = ", ".join(f"{k}×{v}" for k, v in st["runs"].items()) or "0"
        active = f" (running {len(st['active'])}: {', '.join(st['active'])})" if st["active"] else ""
        line2 = (f" ☁ {s.cloud_plan_model if s.cloud_api_key else 'not configured'}: {c['requests']} req "
                 f"in {_k(c['input_tokens'])} out {_k(c['output_tokens'])} │ "
                 f"🔒 {s.worker_model}: {l['requests']} req in {_k(l['input_tokens'])} out {_k(l['output_tokens'])}"
                 f" (incl. judge {st['judge_requests']}) │ 🌐 search {st['web']['search']} fetch {st['web']['fetch']} │ "
                 f"agents {sum(st['runs'].values())} run: {runs}{active}")
        bar.update(line1 + "\n" + line2)
        self._refresh_status()

    # ------------------------------------------------------------ command completion popup
    @property
    def popup_visible(self) -> bool:
        return bool(getattr(self, "_popup_ids", ()))

    @on(TextArea.Changed, "#input")
    def _input_changed(self, ev: TextArea.Changed) -> None:
        self._update_popup()
        self._update_attachments()

    # ------------------------------------------------------------ attachments (paths dropped into the input box)
    def on_paste(self, event: events.Paste) -> None:
        """Textual delivers a paste (and a file dragged onto the terminal) to the focused widget only, so after a
        click on the chat or a log pane a dropped path would vanish; send it to the input box instead.
        Pastes the input box or a dialog's Input handled are stopped there and never reach this."""
        if isinstance(self.screen, ModalScreen) or not event.text:
            return
        event.stop()
        inp = self.query_one("#input", PromptInput)
        inp.focus()
        inp.move_cursor(inp.document.end)
        inp.post_message(events.Paste(event.text))

    def _update_attachments(self) -> None:
        """A dragged file arrives as its path text; show what would be attached so the user sees it before sending."""
        strip = self.query_one("#attachments", Static)
        text = self.query_one("#input", PromptInput).text
        paths = find_paths(text) if text else []
        if not paths:
            if strip.display:
                strip.display = False
            return
        items = []
        for p in paths:
            try:
                items.append(f"{p.name}/" if p.is_dir() else f"{p.name} ({human_size(p.stat().st_size)})")
            except OSError:
                items.append(p.name)
        strip.update(Text("📎 " + " · ".join(items)))
        if not strip.display:
            strip.display = True

    def _load_attachments(self, task: str) -> Material:
        """Read the attached paths (main process, outside the sandbox) into material; the content stays local and goes
        through the same privacy gate as the task text. Called from a thread: PDF / Office extraction can take a while."""
        paths = find_paths(task)
        if not paths:
            return Material()
        s = self.settings
        return load_material(paths, max_file_chars=s.attach_max_file_chars, max_total_chars=s.attach_max_total_chars,
                             deny_names=s.sandbox_deny_names, workspace=s.workspace)

    def _skills_cmd(self, arg: str) -> None:
        store = self.agent.session.skills
        if store is None:
            self._notice("warn", "Skills are off (SKILLS_ENABLED=false)")
            return
        sub, _, sid = arg.partition(" ")
        sid = sid.strip()
        if sub in ("approve", "retire", "restore", "open"):
            try:
                if sub == "open":
                    sk = next(k for k in store.all() if sid and k.id.startswith(sid))
                    self._notice("info", f"{sk.title}: {store.path(sk)}")
                    return
                sk = store.set_status(sid, {"approve": "active", "retire": "retired", "restore": "active"}[sub])
                self._notice("info", f"Skill {sk.id[:8]} is now {sk.status}: {sk.title}")
            except (LookupError, StopIteration):
                self._notice("error", f"No single skill matches {sid!r}")
            return
        store.sync()
        lines = [f"**Skills** (files in `{store.dir}`)"]
        for status in ("active", "candidate", "retired"):
            items = [k for k in store.all() if k.status == status]
            lines.append(f"\n**{status}** ({len(items)})")
            lines += [f"- `{k.id[:8]}` {k.title} · used {k.uses}, helped {k.helped}, confirmed {k.confirmations}"
                      for k in items] or ["- none"]
        lines.append("\n`/skills approve ID` · `/skills retire ID` · `/skills restore ID` · `/skills open ID`")
        self._chat_md("system", "\n".join(lines))

    def _update_popup(self) -> None:
        """Called on every keystroke: only rebuild the list when the candidate set actually changes, and only touch
        display when visibility changes; otherwise every key triggers a full-screen relayout and the UI stutters once
        the chat area fills up."""
        popup = self.query_one("#cmd-popup", OptionList)
        cmds = filter_commands(self.query_one("#input", PromptInput).text)
        if not cmds:
            if popup.display:
                popup.display = False
            self._popup_ids = ()
            return
        ids = tuple(c.name for c in cmds)
        if ids != getattr(self, "_popup_ids", ()):
            popup.clear_options()
            popup.add_options([Option(f"{c.usage}    {c.help}", id=c.name) for c in cmds])
            popup.highlighted = 0
            self._popup_ids = ids
        if not popup.display:
            popup.display = True

    @on(OptionList.OptionSelected, "#cmd-popup")
    def _popup_clicked(self, ev: OptionList.OptionSelected) -> None:
        """Mouse click on a candidate: /model and /voice open their dialog directly; commands with arguments are
        completed; everything else runs immediately."""
        cmd = find_command(str(ev.option.id))
        inp = self.query_one("#input", PromptInput)
        if cmd is None:
            return
        if cmd.name in ("/model", "/voice"):
            inp.clear()
            self.query_one("#cmd-popup", OptionList).display = False
            self._popup_ids = ()
            self._slash(cmd.name)
        elif cmd.takes_args:
            inp.text = cmd.name + " "
            inp.move_cursor(inp.document.end)
            inp.focus()
            self._update_popup()
        else:
            inp.clear()
            self.query_one("#cmd-popup", OptionList).display = False
            self._popup_ids = ()
            self._slash(cmd.name)

    def popup_key(self, key: str) -> bool:
        """Handle up/down, Tab, Esc and Enter while the popup is visible; returns whether the key was handled."""
        popup = self.query_one("#cmd-popup", OptionList)
        if key == "up":
            popup.action_cursor_up()
        elif key == "down":
            popup.action_cursor_down()
        elif key == "escape":
            popup.display = False
            self._popup_ids = ()
        elif key in ("tab", "enter"):
            opt = popup.highlighted_option
            cmd = find_command(str(opt.id)) if opt is not None else None
            if cmd is None:
                return False
            inp = self.query_one("#input", PromptInput)
            head = inp.text.strip().partition(" ")[0]
            if key == "enter" and (head == cmd.name or not cmd.takes_args or cmd.name in ("/model", "/voice")):
                if head != cmd.name:
                    inp.text = cmd.name
                return False              # hand over to the normal Enter submit
            inp.text = cmd.name + (" " if cmd.takes_args else "")
            inp.move_cursor(inp.document.end)
            self._update_popup()
        else:
            return False
        return True

    def _bind_record_key(self, new: str, old: Optional[str] = None) -> None:
        """The record key is configurable: drop the old binding, add the new one (priority binding so the input
        box's own shortcuts cannot swallow it)."""
        old = old or self.settings.voice_key
        self._bindings.key_to_bindings.pop(old, None)
        self._bindings._add_binding(Binding(new, "toggle_record", "Record", priority=True))
        self.settings.voice_key = new
        self.refresh_bindings()
        self.query_one("#hints", Static).update(self._hints_text())

    def _save_env(self, values: dict[str, str]) -> None:
        try:
            update_env(self.settings.env_path, values)
            self._notice("info", "Written to .env: " + ", ".join(f"{k}={v}" for k, v in values.items()))
        except OSError as e:
            self._notice("warn", f"Failed to write .env: {e}")

    # ------------------------------------------------------------ input
    @on(PromptInput.Submitted)
    def submitted(self, ev: PromptInput.Submitted) -> None:
        self.query_one("#cmd-popup", OptionList).display = False
        self._popup_ids = ()
        value = ev.value
        if is_command(value):
            self._slash(value)
            return
        self._start_task(value, Force.NONE)

    def _start_task(self, task: str, force: Force, business: bool = False) -> None:
        if self._busy:
            self._notice("warn", "The previous task is still running; press Esc to interrupt it before sending.")
            return
        self._update_attachments()
        self._busy = True
        self._routed, self._running_cmd, self._waiting_user, self._step = False, None, None, None
        self._log("[b]▶ Task started[/b]")
        self._refresh_topbar()
        self._timer = self.set_interval(1.0, self._refresh_topbar)
        self.query_one("#plan", Static).update("")
        self.run_worker(self._run_task(task, force, business), group="task", exclusive=True, exit_on_error=False)

    async def _run_task(self, task: str, force: Force, business: bool = False) -> None:
        try:
            m = await asyncio.to_thread(self._load_attachments, task)
            for n in m.notes:
                self._notice("warn", f"Attachment: {n}")
            self._pending_attachments = m.summary
            await self.agent.run(task, m.text, force=force, read_roots=m.roots, business=business)
        except asyncio.CancelledError:
            self.agent.cancel_running()
            self._notice("warn", "Interrupted. Add instructions to continue.", announce=True)
            self._finish(f"⏹ Interrupted at {time.strftime('%H:%M')} · send instructions to continue", "stopped")
            raise
        except Exception as e:
            self._notice("error", f"Task failed: {type(e).__name__}: {e}", announce=True)
            self._finish(f"✖ Task failed at {time.strftime('%H:%M')}: {type(e).__name__}", "failed")
        finally:
            self._busy = False
            if getattr(self, "_timer", None):
                self._timer.stop()
                self._timer = None
            self._end_stream()
            self._running_cmd = self._waiting_user = self._step = None
            self._refresh_status()
            self._refresh_topbar()

    def action_interrupt(self) -> None:
        if self.recorder.recording:   # while recording, Esc only cancels the recording
            self._stop_rec_timer()
            self.recorder.cancel()
            self._notice("info", "Recording cancelled")
            self._refresh_topbar()
            return
        if self._busy:
            self.workers.cancel_group(self, "task")
            self.agent.cancel_running()

    # ------------------------------------------------------------ voice
    def action_toggle_record(self) -> None:
        if self._transcribing:
            self._notice("warn", "Still transcribing the previous recording, please wait.")
            return
        if self.recorder.recording:
            self._stop_rec_timer()
            try:
                audio = self.recorder.stop()
            except Exception as e:
                self._notice("error", f"Recording failed: {e}")
                return
            self._transcribing = True
            self._refresh_topbar()
            self._transcribe(audio)
            return
        try:
            self.recorder.start()
        except VoiceUnavailable as e:
            # Once in the chat; repeated presses (F5 is also the Mac dictation key) only get a passing toast
            msg = f"{self.settings.voice_key.upper()} is the voice record key, but voice input is unavailable: {e}. " \
                  "Change the key with /voice key KEY"
            if msg in self._voice_warned:
                self.notify(msg, severity="warning", timeout=3)
            else:
                self._voice_warned.add(msg)
                self._notice("warn", msg)
            return
        except Exception as e:
            self._notice("error", f"Could not start recording: {e}")
            return
        self._rec_timer = self.set_interval(1.0, self._tick_recording)
        self._refresh_topbar()

    def _tick_recording(self) -> None:
        if self.recorder.recording and getattr(self.recorder, "auto_stopped", False):
            self._notice("info", f"Recording hit the {int(self.settings.voice_max_seconds)}s limit; stopped and transcribing")
            self.action_toggle_record()
            return
        self._refresh_topbar()

    def _stop_rec_timer(self) -> None:
        if self._rec_timer is not None:
            self._rec_timer.stop()
            self._rec_timer = None

    @work(thread=True, exclusive=True, group="transcribe")
    def _transcribe(self, audio) -> None:
        try:
            text = self.transcriber.transcribe(audio)
        except VoiceUnavailable as e:
            self.call_from_thread(self._notice, "warn", f"Speech recognition unavailable: {e}")
            text = None
        except Exception as e:
            self.call_from_thread(self._notice, "error", f"Speech recognition error: {type(e).__name__}: {e}")
            text = None
        self.call_from_thread(self._transcribed, text)

    def _transcribed(self, text: Optional[str]) -> None:
        self._transcribing = False
        if text is not None:
            if is_blank_transcript(text):
                self._notice("warn", "Didn't catch that, please try again.")
            else:
                inp = self.query_one("#input", PromptInput)
                inp.text = (inp.text.rstrip() + " " + text).strip() if inp.text.strip() else text
                inp.move_cursor(inp.document.end)
                inp.focus()
        self._refresh_topbar()

    def action_toggle_voice(self) -> None:
        self.speaker.enabled = not self.speaker.enabled
        self._notice("info", "Speech output on" if self.speaker.enabled else "Speech output off")
        self._save_env({"VOICE_OUTPUT": "true" if self.speaker.enabled else "false"})
        self._refresh_topbar()

    def _model_cmd(self, arg: str) -> None:
        s = self.settings
        which = {"local": "worker", "worker": "worker", "judge": "judge", "cloud": "cloud", "plan": "plan"}
        if arg == "list":
            try:
                names = self.agent.list_local_models()
            except Exception as e:
                self._notice("warn", f"Failed to read the Ollama model list: {e}")
                return
            lines = [f"- `{n}`" + ("  ← executor" if n == s.worker_model else "") + ("  ← judge" if n == s.judge_model else "")
                     for n in names]
            self._chat_md("system", "**Installed Ollama models** (switch with `/model local NAME` or `/model judge NAME`)\n\n"
                          + ("\n".join(lines) or "No models; run ollama pull first"))
            return
        role, _, name = arg.partition(" ")
        name = name.strip()
        if role in which and name:
            changed = self.agent.set_models(**{which[role]: name})
            if not changed:
                self._notice("info", "Models unchanged")
                return
            if which[role] in ("worker", "judge"):
                self._check_model_pulled(name)
            self._notice("info", "Switched: " + ", ".join(f"{k}={v}" for k, v in changed.items()))
            self._save_env(changed)
            self._refresh_topbar()
        elif arg:
            self._notice("warn", "Usage: /model (dialog) · /model list · /model local|judge|cloud|plan NAME")
        else:
            self._open_model_dialog()

    @work(thread=True, exclusive=True, group="model-dialog")
    def _open_model_dialog(self) -> None:
        """Asking Ollama for the model list can take a few seconds; do it in a thread and open the dialog afterwards
        so the UI does not freeze."""
        try:
            local = self.agent.list_local_models()
        except Exception:
            local = []
        self.call_from_thread(self.push_screen, ModelScreen(self.settings, local), self._apply_model_config)

    @work(thread=True, group="model-check")
    def _check_model_pulled(self, name: str) -> None:
        try:
            if name not in self.agent.list_local_models():
                self.call_from_thread(self._notice, "warn", f"{name} is not installed in Ollama; run ollama pull {name} before first use")
        except Exception:
            pass

    def _apply_model_config(self, result: Optional[dict]) -> None:
        if not result:
            return
        changed = self.agent.set_models(**result)
        if not changed:
            self._notice("info", "Models unchanged")
            return
        self._notice("info", "Switched: " + ", ".join(f"{k}={v}" for k, v in changed.items()))
        self._save_env(changed)
        self._refresh_topbar()

    def _apply_voice_config(self, result: Optional[dict]) -> None:
        if not result:
            return
        sp, s = self.speaker, self.settings
        values = {}
        if result["enabled"] != sp.enabled:
            sp.enabled = result["enabled"]
            values["VOICE_OUTPUT"] = "true" if sp.enabled else "false"
        if result["voice"] and result["voice"] != sp.voice:
            sp.voice = result["voice"]
            values["VOICE_NAME"] = sp.voice
        if result["rate"] != int(s.voice_rate or 0):
            s.voice_rate = sp.rate = result["rate"]
            values["VOICE_RATE"] = str(result["rate"])
        if result["key"] != s.voice_key:
            self._bind_record_key(result["key"])
            values["VOICE_KEY"] = result["key"]
        if values:
            self._save_env(values)
        else:
            self._notice("info", "Voice configuration unchanged")
        self._refresh_topbar()

    def _voice_cmd(self, arg: str) -> None:
        sp = self.speaker
        if arg.startswith("key"):
            key = arg[3:].strip().lower()
            if not valid_record_key(key):
                self._notice("warn", "The record key must be one of F3-F12 or ctrl+letter (ctrl+b/r/q/j/c and F2/F6 are taken), e.g. /voice key f8")
                return
            self._bind_record_key(key)
            self._notice("info", f"Record key changed to {key.upper()}")
            self._save_env({"VOICE_KEY": key})
        elif arg in ("on", "off"):
            sp.enabled = arg == "on"
            self._notice("info", "Speech output on" if sp.enabled else "Speech output off")
            self._save_env({"VOICE_OUTPUT": "true" if sp.enabled else "false"})
        elif arg == "list":
            voices = sp.list_voices()
            lines = [f"- `{name}` ({lang}){'  ← current' if name == sp.voice else ''}" for name, lang in voices]
            self._chat_md("system", "**Available voices** (switch with `/voice NAME`)\n\n" + ("\n".join(lines) or "No voices found"))
        elif arg == "test":
            sp.preview("Test of the speech output.")   # ignores the on/off switch
        elif arg:
            if sp.set_voice(arg):
                self._notice("info", f"Switched to {sp.voice}")
                sp.preview(f"Switched to {sp.voice}")
                self._save_env({"VOICE_NAME": sp.voice})
            else:
                self._notice("warn", f"No voice named {arg}; /voice list shows the available voices")
        else:
            self.push_screen(VoiceScreen(self.settings, sp), callback=self._apply_voice_config)
        self._refresh_topbar()

    def action_cycle_mode(self) -> None:
        new = RunMode.AUTO if self.settings.mode is RunMode.DEFAULT else RunMode.DEFAULT
        self.agent.set_mode(new)
        self._refresh_topbar()

    def action_toggle(self, selector: str) -> None:
        w = self.query_one(selector)
        w.display = not w.display

    # ------------------------------------------------------------ slash commands
    def _slash(self, line: str) -> None:
        cmd, _, arg = line.partition(" ")
        arg = arg.strip()
        if cmd == "/help":
            self._chat_md("system", help_markdown(self.settings.voice_key))
        elif cmd == "/model":
            self._model_cmd(arg)
        elif cmd == "/mode":
            modes = {"default": RunMode.DEFAULT, "auto": RunMode.AUTO}
            if arg not in modes:
                self._notice("warn", "Usage: /mode default|auto (no-sandbox mode can only be started with --dangerously-no-sandbox)")
                return
            self.agent.set_mode(modes[arg])
            self._refresh_topbar()
        elif cmd in ("/local", "/cloud"):
            if not arg:
                self._notice("warn", f"Usage: {cmd} TASK")
                return
            self._start_task(arg, Force.LOCAL if cmd == "/local" else Force.CLOUD)
        elif cmd == "/data":
            if not arg:
                self._notice("warn", "Usage: /data QUESTION")
                return
            self._start_task(arg, Force.NONE, business=True)
        elif cmd == "/apps":
            self.run_worker(self._apps_cmd(arg), group="apps", exclusive=True, exit_on_error=False)
        elif cmd == "/ga4":
            self.run_worker(self._ga4_cmd(arg), group="ga4", exclusive=True, exit_on_error=False)
        elif cmd == "/new":
            if self._busy:
                self._notice("warn", "The task is still running; /new after it finishes or press Esc")
                return
            self.agent.new_session()
            self._refresh_status()
        elif cmd == "/outbound":
            self.query_one("#right", TabbedContent).active = "tab-outbound"
            self.query_one("#right").display = True
        elif cmd == "/perf":
            self.query_one("#right", TabbedContent).active = "tab-perf"
            self.query_one("#right").display = True
        elif cmd == "/snapshots":
            if arg.startswith("prune"):
                keep = int(arg.split()[1]) if len(arg.split()) > 1 and arg.split()[1].isdigit() else 5
                dropped = self.agent.session.snapshots.prune(keep)
                self._notice("info", f"Pruned {len(dropped)} snapshots, kept the latest {keep}")
                return
            snaps = self.agent.session.snapshots.list()[-10:]
            lines = [f"- `{x.id}` {x.kind} {x.label} {time.strftime('%m-%d %H:%M:%S', time.localtime(x.ts))}"
                     for x in snaps]
            self._chat_md("system", "**Snapshots** (latest 10; `/snapshots prune [N]` keeps only the latest N)\n\n"
                          + ("\n".join(lines) or "None yet"))
        elif cmd == "/rollback":
            if self._busy:
                self._notice("warn", "A task is running; press Esc to interrupt it first.")
                return
            try:
                sid = self.agent.rollback(arg or None)
                self._show_diff(sid)
            except Exception as e:
                self._notice("error", f"Rollback failed: {e}")
        elif cmd == "/usage":
            st = self.agent.session.stats.snapshot()
            c, l = st["cloud"], st["local"]
            rows = [f"| ☁ {self.settings.cloud_label} (billed) | {c['requests']} | {c['input_tokens']:,} | {c['output_tokens']:,} |",
                    f"| 🔒 Ollama (local, incl. {st['judge_requests']} judge requests) | {l['requests']} | "
                    f"{l['input_tokens']:,} | {l['output_tokens']:,} |"]
            runs = "\n".join(f"- {k}: {v}" for k, v in st["runs"].items()) or "- no agent has run yet"
            self._chat_md("system", "**Usage**\n\n| Model | Requests | Input tokens | Output tokens |\n|---|---|---|---|\n"
                          + "\n".join(rows) + f"\n\n**agent runs** (total {sum(st['runs'].values())})\n\n{runs}"
                          + f"\n\n**Web**: {st['web']['search']} searches, {st['web']['fetch']} fetches"
                          + f"\n\nSession running for {_mmss(st['session_elapsed_s'])}"
                          + (f", current task {_mmss(st['task_elapsed_s'])}" if st["task_elapsed_s"] is not None else "")
                          + (f", waiting for {st['current']}" if st["current"] else ""))
        elif cmd == "/skills":
            self._skills_cmd(arg)
        elif cmd == "/calibrate":
            from .. import calibrate
            from ..config import PROJECT_ROOT
            report = calibrate.build_report(self.settings, eval_signals=PROJECT_ROOT / "evals" / "signals.jsonl")
            self._chat_md("system", calibrate.format_report(report))
        elif cmd == "/export":
            path = Path(arg).expanduser() if arg else \
                self.settings.data_dir / "exports" / time.strftime("session-%Y%m%d-%H%M%S.md")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n\n".join(f"### {role}\n\n{text}" for role, text in self.transcript), encoding="utf-8")
            self._notice("info", f"Session exported to {path} (contains original text; handle with care)")
        elif cmd == "/voice":
            self._voice_cmd(arg)
        elif cmd == "/clear":
            self.query_one("#chat").remove_children()
        else:
            self._notice("warn", f"Unknown command {cmd}; /help lists all commands")

    async def action_quit(self) -> None:
        try:
            self.speaker.close()
        except Exception:
            pass
        await self._close_agent()
        self.exit()

    async def _close_agent(self) -> None:
        """Stop connector processes. Idempotent; runs on every exit path (quit key, app.exit(), on_unmount)."""
        if self._agent is not None:
            try:
                await self._agent.aclose()
            except Exception:
                pass

    async def on_unmount(self) -> None:
        await self._close_agent()

    # ------------------------------------------------------------ approval
    async def _approve(self, req: ApprovalRequest) -> Approval:
        if phrase := phrase_for(req):
            self.speaker.speak(phrase)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.push_screen(ApprovalScreen(req), callback=lambda r: fut.done() or fut.set_result(r))
        answer = await self._wait_user(f"approve {req.tool}: {req.summary[:80]}", fut)
        self._log(f"  ↳ you answered: {answer.value}")
        if answer is Approval.DENY:
            self._running_cmd = None   # a denied command never runs, so it never reports back
        return answer

    async def _provide_input(self, req: InputRequest) -> Optional[str]:
        if phrase := phrase_for(req):
            self.speaker.speak(phrase)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.push_screen(InputScreen(req), callback=lambda r: fut.done() or fut.set_result(r))
        answer = await self._wait_user(f"type input for {req.command[:80]}", fut)
        self._log("  ↳ you stopped the command" if answer is None else "  ↳ you typed a reply")
        return answer

    async def _choose(self, req: ChoiceRequest) -> Optional[str]:
        if phrase := phrase_for(req):
            self.speaker.speak(phrase)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.push_screen(ChoiceScreen(req), callback=lambda r: fut.done() or fut.set_result(r))
        answer = await self._wait_user(f"pick: {req.prompt}", fut)
        self._log("  ↳ you cancelled the choice" if answer is None else f"  ↳ you chose {escape(answer)}")
        return answer

    async def _clarify(self, req: ClarifyRequest) -> Optional[list[ClarifyAnswer]]:
        if phrase := phrase_for(req):
            self.speaker.speak(phrase)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.push_screen(ClarifyScreen(req), callback=lambda r: fut.done() or fut.set_result(r))
        answer = await self._wait_user("planner questions", fut)
        if answer is None:
            # Ctrl+X in the dialog: stop the task. Not action_interrupt(): while recording it only cancels the
            # recording and the task would go on.
            self._log("  ↳ you stopped the task")
            self.workers.cancel_group(self, "task")
            self.agent.cancel_running()
            return []
        self._log(f"  ↳ you answered {len(answer)} question(s)")
        return answer

    async def _review_plan(self, req: PlanReviewRequest) -> PlanDecision:
        if phrase := phrase_for(req):
            self.speaker.speak(phrase)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.push_screen(PlanScreen(req), callback=lambda r: fut.done() or fut.set_result(r))
        decision = await self._wait_user("plan review", fut)
        self._log(f"  ↳ plan {decision.action}")
        return decision

    async def _wait_user(self, what: str, fut: asyncio.Future):
        self._waiting_user = (what, time.monotonic())
        self._log(f"[b red]⏸ Waiting for you:[/] {escape(what)}")
        self._refresh_status()
        try:
            return await fut
        finally:
            self._waiting_user = None
            self._refresh_status()

    # ------------------------------------------------------------ event rendering
    async def on_core_event(self, msg: CoreEvent) -> None:
        ev = msg.event
        log = self.query_one("#log", RichLog)
        if phrase := phrase_for(ev):
            self.speaker.speak(phrase)
        if isinstance(ev, ChatMessage):
            await self._on_chat(ev)
        elif isinstance(ev, RouteDecided):
            self._end_stream()
            self._routed = True
            self._log(f"⇢ Routed: [b]{Route(ev.route).label}[/b]")
            label = Route(ev.route).label
            sig = ev.signals
            priv = sig.get("privacy", {})
            lines = [f"[b]Route: {label}[/b]", *(f"· {escape(r)}" for r in ev.reasons), "",
                     f"Privacy: {'[red]sensitive[/red]' if priv.get('sensitive') else '[green]none[/green]'} "
                     f"{escape(str(priv.get('entities') or ''))}"]
            if "task_type" in sig:
                lines.append(f"Type: {sig['task_type']['choice']} ({sig['task_type']['confidence']:.2f})")
                lines.append(f"Difficulty: {sig['complexity']['score']} ({sig['complexity']['confidence']:.2f})")
                if sig.get("routellm_win_rate") is not None:
                    lines.append(f"RouteLLM: {sig['routellm_win_rate']:.2f}")
            self.query_one("#route", Static).update("\n".join(lines))
        elif isinstance(ev, PlanUpdated):
            lines = []
            if ev.outline:
                lines.append("[b]Plan[/b]")
                lines += [f"  {i} {escape(x[:60])}" for i, x in enumerate(ev.outline, 1)]
                lines.append("")
            lines.append("[b]delegated[/b]" if ev.outline else "[b]Plan[/b]")
            for i, (step, done) in enumerate(zip(ev.steps, ev.done), 1):
                mark = "[green]✓[/green]" if done else ("[yellow]●[/yellow]" if i == len(ev.steps) else "✗")
                lines.append(f"{mark} {i} {escape(step[:60])}")
            self.query_one("#plan", Static).update("\n".join(lines))
        elif isinstance(ev, PlanProposed):
            if ev.approved_by == "auto":
                self._notice("info", f"Plan accepted automatically ({len(ev.plan.get('steps', []))} steps); "
                                     "it is in PLAN.md")
            self._log(f"📋 Plan rev {ev.revision} accepted ({ev.approved_by})")
        elif isinstance(ev, ReviewArrived):
            head = f"**🔍 Local review · round {ev.round} · {'passed' if ev.passed else 'failed'}**"
            if not ev.passed and not ev.final:
                head += " (returned to the executor for fixes)"
            body = [head] + [f"- ✗ {x}" for x in ev.problems] + [f"- → {x}" for x in ev.suggestions]
            self._chat_md("report", "\n".join(body))
            self._log(f"🔍 Review round {ev.round}: {'[green]passed[/]' if ev.passed else '[red]failed[/]'}")
        elif isinstance(ev, CommandStarted):
            risk = f" [{'red' if ev.risk == 'high' else 'dim'}]risk:{ev.risk}[/]" if ev.risk else ""
            self._running_cmd = (ev.tool, ev.summary, time.monotonic())
            self._log(f"[b cyan]$ {escape(ev.tool)}[/] {escape(ev.summary[:300])}{risk} [dim](running)[/]")
            self._refresh_status()
        elif isinstance(ev, CommandFinished):
            color = "green" if ev.exit_code == 0 else "red"
            out = "\n".join(ev.output.splitlines()[:40])
            self._running_cmd = None
            self._log(f"  [{color}]{'✓' if ev.exit_code == 0 else '✗'} {escape(ev.tool)} finished · exit={ev.exit_code}[/] "
                      f"{ev.duration_s}s")
            self._refresh_status()
            if out.strip():
                log.write(Text(out, style="dim"))
        elif isinstance(ev, OutboundSent):
            icon = "🌐" if ev.model.startswith("web:") else "☁"
            self.query_one("#outbound", RichLog).write(Text.from_markup(
                f"[b yellow]{icon} → {escape(ev.model)}[/] {time.strftime('%H:%M:%S')}"))
            self.query_one("#outbound", RichLog).write(Text(ev.content))
            self._refresh_topbar()
        elif isinstance(ev, OutboundBlocked):
            self._notice("error", f"Outbound guard blocked: {ev.reason}")
        elif isinstance(ev, ReportArrived):
            r = ev.report
            body = [f"**🔒 Executor report** · {r.get('status')}" + (" (stripped, status only)" if ev.stripped else "")]
            body += [f"- {x}" for x in r.get("steps_done", [])]
            body += [f"- 📄 `{a['path']}` ({a['type']}, {a['size_hint']})" for a in r.get("artifacts", [])]
            body += [f"- ✔ {x}" for x in r.get("verification", [])]
            body += [f"- ⚠ {x}" for x in r.get("issues", [])]
            if lr := r.get("local_review"):
                body.append(f"- 🔍 Local review: {'passed' if lr.get('passed') else 'failed'}")
            if r.get("diagnosis"):
                body.append(f"- 🩺 Diagnosis: {r['diagnosis']}")
            if r.get("question"):
                body.append(f"- ❓ {r['question']}")
            self._chat_md("report", "\n".join(body))
        elif isinstance(ev, Tainted):
            self._log(f"[b red]⚑ Session tainted: {escape(ev.reason)}[/]")
        elif isinstance(ev, SnapshotTaken):
            if ev.label == "task":   # the "Changes" tab and default rollback use the pre-task snapshot; step snapshots are for the reviewer only
                self._last_snapshot = ev.snapshot_id
            self._log(f"[dim]📸 Snapshot {ev.snapshot_id} ({ev.kind}, {'pre-task' if ev.label == 'task' else 'pre-step'})[/]")
        elif isinstance(ev, StatsUpdated):
            self._log_replies(ev.stats)
            self._refresh_topbar()
            self._refresh_status()
        elif isinstance(ev, ExecutorProgress):
            if ev.started:
                self._log(f"▶ Executor step started (limit {_mmss(ev.limit_s)})")
            elif ev.done:
                self._log(f"■ Executor step ended · {ev.tool_calls} tool calls")
            self._step = None if ev.done else ev
            self._refresh_status()
        elif isinstance(ev, Notice):
            self._notice(ev.level, ev.text)
            if ev.level in ("warn", "error"):
                self._log(f"[{'yellow' if ev.level == 'warn' else 'red'}]{'⚠' if ev.level == 'warn' else '✖'} "
                          f"{escape(ev.text)}[/]")
            self._refresh_topbar()
        elif isinstance(ev, TaskFinished):
            self._end_stream()
            route = Route(ev.route).label if ev.route in Route._value2member_map_ else ev.route
            icon, word, style = _FINISH.get(ev.status, ("✔", "Finished", "done"))
            self._notice("info", f"{icon} {word} · {route} · {ev.backend} · outbound {ev.outbound_count} · "
                                 f"took {_mmss(ev.elapsed_s)}")
            self._finish(f"{icon} {word} at {time.strftime('%H:%M')} ({route}, took {_mmss(ev.elapsed_s)}) · "
                         f"ready for the next task", style)
            if self._last_snapshot:
                self._show_diff(self._last_snapshot)

    # ------------------------------------------------------------ status: what is happening right now
    def _state(self, st: Optional[dict] = None) -> tuple[str, str]:
        """(text, style) of the one thing the app is doing now. Order matters: waiting for the user beats everything,
        then a running command, then a model reply being awaited; when nothing is running, say whether the last task
        finished and how, or that the app is ready."""
        st = st or self.agent.session.stats.snapshot()
        now = time.monotonic()
        if self.recorder.recording:
            return f"🎙 Recording {_mmss(self.recorder.elapsed_s)} · F5 to stop, Esc to cancel", "busy"
        if self._transcribing:
            return "🎙 Transcribing the recording...", "busy"
        if self._waiting_user:
            what, since = self._waiting_user
            return f"⏸ Waiting for YOU ({_mmss(now - since)}): {what} · the task is paused", "waiting"
        if self._busy:
            step = ""
            if self._step:
                s = self._step
                step = f" · step {_mmss(s.elapsed_s)} of {_mmss(s.limit_s)}, {s.tool_calls} tool calls"
            if self._running_cmd:
                tool, summary, since = self._running_cmd
                return f"▶ Running {tool} ({_mmss(now - since)}): {summary[:70]}{step}", "busy"
            if st.get("current"):
                waited = _mmss(st["current_elapsed_s"] or 0)
                who = _ROLE_DOING.get(st.get("current_role") or "", "Waiting for a model reply")
                if st["current"].startswith("🌐"):
                    return f"{st['current']} ({waited}){step}", "busy"
                return f"{who} · {st['current']} · {waited} so far{step}", "busy"
            if not self._routed:
                return "⏳ Routing: privacy check and task classification (local judge model)", "busy"
            return f"⏳ Working: checks and bookkeeping between steps{step}", "busy"
        if self.agent.learning:
            return "📚 Task finished · learning from it in the background (you can send the next task)", "done"
        if self._last_result:
            return self._last_result
        return "● Ready for a task", "ready"

    async def _apps_cmd(self, arg: str) -> None:
        try:
            if arg:
                name = await self.agent.set_default_app(arg)
                self._chat_md("system", f"Default app: {name}")
            else:
                names = await self.agent.list_apps()
                self._chat_md("system", "Your apps:\n" + "\n".join(f"- {n}" for n in names))
        except LookupError as e:
            self._notice("warn", str(e))
        except Exception as e:
            self._notice("error", f"/apps failed: {type(e).__name__}: {e}")

    async def _ga4_cmd(self, arg: str) -> None:
        try:
            if arg:
                name = await self.agent.set_default_property(arg)
                self._chat_md("system", f"Default GA4 property: {name}")
            else:
                names = await self.agent.list_properties()
                if names:
                    self._chat_md("system", "Your GA4 properties:\n" + "\n".join(f"- {n}" for n in names))
                else:
                    self._chat_md("system", "No GA4 properties are visible to this Google account.")
        except LookupError as e:
            self._notice("warn", str(e))
        except Exception as e:
            from ..connectors.ga4 import setup_error
            log.warning("/ga4 failed (%s)", type(e).__name__)
            # a chat message, not an error notice: notices are spoken aloud, and this text is Google's setup message
            self._chat_md("system", f"/ga4 could not list your GA4 properties: {setup_error(e)}")

    def _refresh_status(self) -> None:
        try:
            line = self.query_one("#status", Static)
        except NoMatches:   # timer firing during teardown
            return
        text, style = self._state()
        if self.agent.session.business:
            text += " · business-locked (/new to lift)"
        for cls in ("ready", "busy", "waiting", "done", "partial", "failed", "stopped"):
            line.set_class(cls == style, cls)
        line.update(Text(text))

    def _tick_status(self) -> None:
        learning = self.agent.learning
        if self._was_learning and not learning:
            self._log("📚 Background learning finished")
        self._was_learning = learning
        self._refresh_status()

    def _finish(self, text: str, style: str) -> None:
        self._last_result = (text, style)
        self._log(f"[b]■ {escape(text.split(' · ready')[0])}[/b]")
        self._refresh_status()

    def _log(self, markup: str) -> None:
        """One line in the Log tab, always with the time, so it reads as a timeline."""
        try:
            self.query_one("#log", RichLog).write(Text.from_markup(f"[dim]{time.strftime('%H:%M:%S')}[/] {markup}"))
        except NoMatches:
            pass

    def _log_replies(self, stats: dict) -> None:
        """Log each finished model reply once: who, how long, how many tokens. This is where the time goes."""
        for r in stats.get("replies", []):
            if r["seq"] <= self._replies_seen:
                continue
            self._replies_seen = r["seq"]
            who = r["role"] or "model"
            self._log(f"[magenta]{escape(r['model'])}[/] {who} reply · {_mmss(r['secs'])} · "
                      f"in {_k(r['in'])} / out {_k(r['out'])} tokens")

    async def _on_chat(self, ev: ChatMessage) -> None:
        if ev.role == "planner" and ev.streaming:
            if self._stream is None:
                self._stream_md = Markdown("", classes="msg planner")
                self._stream_md.border_title = f"☁ Planner ({self.settings.cloud_label})"
                await self.query_one("#chat").mount(self._stream_md)
                self._stream = Markdown.get_stream(self._stream_md)
                self._stream_buf = ""
            self._stream_buf += ev.text
            await self._stream.write(ev.text)
            self.query_one("#chat").scroll_end(animate=False)
            return
        if ev.role == "planner" and self._stream_md is not None:  # stream ended: replace with the final text (placeholders restored)
            md = self._stream_md
            self._end_stream()
            await md.update(ev.text)
            self.transcript.append(("Planner", ev.text))
            return
        self._end_stream()
        role = {"user": "user", "executor": "executor", "planner": "planner"}.get(ev.role, "system")
        if role == "user":
            shown = ev.text
            attached = self._pending_attachments
            if attached:
                shown += "\n📎 " + "\n📎 ".join(attached)
                self._pending_attachments = []
            w = Static(Text("› " + shown), classes="msg user")
            await self.query_one("#chat").mount(w)
            self.transcript.append(("You", shown))
        else:
            self._chat_md(role, ev.text)
        self.query_one("#chat").scroll_end(animate=False)

    def _end_stream(self) -> None:
        if self._stream is not None:
            asyncio.ensure_future(self._stream.stop())
            if self._stream_buf:
                self.transcript.append(("Planner", self._stream_buf))
        self._stream, self._stream_md, self._stream_buf = None, None, ""

    def _chat_md(self, role: str, text: str) -> None:
        titles = {"executor": "🔒 Local executor", "planner": f"☁ Planner ({self.settings.cloud_label})", "report": "",
                  "system": "ℹ"}
        md = Markdown(text, classes=f"msg {role}")
        if titles.get(role):
            md.border_title = titles[role]
        self.query_one("#chat").mount(md)
        self.query_one("#chat").scroll_end(animate=False)
        self.transcript.append((titles.get(role) or role, text))

    def _notice(self, level: str, text: str, *, announce: bool = False) -> None:
        if announce and (phrase := phrase_for(Notice(level, text))):
            self.speaker.speak(phrase)
        w = Static(Text(("⚠ " if level == "warn" else "✖ " if level == "error" else "· ") + text),
                   classes=f"notice {level}")
        self.query_one("#chat").mount(w)
        self.query_one("#chat").scroll_end(animate=False)

    def _show_diff(self, snapshot_id: str) -> None:
        diff = self.agent.diff_since(snapshot_id)
        log = self.query_one("#diff", RichLog)
        log.clear()
        log.write(Text(f"Changes since snapshot {snapshot_id} (/rollback {snapshot_id} to revert)", style="bold"))
        log.write(Syntax(diff, "diff") if diff.strip() else Text("(no changes)", style="dim"))

    async def _tick_perf(self) -> None:
        """Sample one frame per second (ioreg is a subprocess, so run it in a thread to keep the UI responsive) and
        refresh the "Perf" tab."""
        try:
            s = await asyncio.to_thread(self.perf.sample)
        except Exception:   # a failed sample only affects this tab, not the task
            return
        # The sample takes a thread hop; by the time it returns the panel may be mid-mount or the app tearing down.
        try:
            self.query_one(PerfPanel).show(s, self.perf.cpu_history, self.perf.gpu_history)
        except NoMatches:
            pass
