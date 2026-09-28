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
import re
import time
from pathlib import Path
from typing import Optional

from rich.markup import escape
from rich.syntax import Syntax
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import (Button, Input, Label, Markdown, OptionList, RichLog, Select, Static, Switch, TabbedContent,
                             TabPane, TextArea)
from textual.widgets.option_list import Option

from ..attachments import Material, find_paths, human_size, load_material
from ..config import RunMode, Settings, update_env
from ..perf import PerfSample, PerfSampler, render_graph
from .commands import filter_commands, find_command, help_markdown
from ..voice import Recorder, Speaker, Transcriber, VoiceUnavailable, is_blank_transcript, phrase_for
from ..events import (Approval, ApprovalRequest, ChatMessage, CommandFinished, CommandStarted, Event, Notice,
                      OutboundBlocked, OutboundSent, PlanUpdated, ReportArrived, ReviewArrived, RouteDecided,
                      SnapshotTaken, StatsUpdated, Tainted, TaskFinished)
from ..policy import Force, Route




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
            yield Label("Cloud direct model (DeepSeek)")
            yield Input(self.s.deepseek_model, id="cfg-cloud")
            yield Label("Cloud planner model (DeepSeek)")
            yield Input(self.s.deepseek_plan_model, id="cfg-plan")
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
        if self.settings.voice_key != "f5":
            self._bind_record_key(self.settings.voice_key, "f5")
        self._refresh_topbar()
        self._perf_timer = self.set_interval(1.0, self._tick_perf)
        self.query_one("#input").focus()
        if self.settings.mode is RunMode.NO_SANDBOX:
            self._notice("error", "No-sandbox mode: the executor can access the network and read/write the whole disk. The privacy gate is still on.")
        if not self.settings.deepseek_api_key:
            self._notice("warn", "DEEPSEEK_API_KEY not set: plan mode and cloud direct will fall back to local execution.")
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
        now = st["current"] or ("⏳ waiting for tool/sandbox" if self._busy else "idle")
        if st["current"] and st["current_elapsed_s"] is not None:
            now += f" (waited {_mmss(st['current_elapsed_s'])})"
        if self.recorder.recording:
            now = f"🎙 Recording... {_mmss(self.recorder.elapsed_s)} (F5 to stop, Esc to cancel)"
        elif self._transcribing:
            now = "🎙 Transcribing..."
        task_t = f" │ task {_mmss(st['task_elapsed_s'])}" if st["task_elapsed_s"] is not None else ""
        voice = "🔊" if self.speaker.enabled else "🔇"
        line1 = (f" {s.mode.label} │ {voice} │ outbound {n}, all passed the gate │ current: {now}{task_t} │ "
                 f"session {_mmss(st['session_elapsed_s'])} │ {s.workspace}")
        c, l = st["cloud"], st["local"]
        runs = ", ".join(f"{k}×{v}" for k, v in st["runs"].items()) or "0"
        active = f" (running {len(st['active'])}: {', '.join(st['active'])})" if st["active"] else ""
        line2 = (f" ☁ {s.deepseek_plan_model if s.deepseek_api_key else 'not configured'}: {c['requests']} req "
                 f"in {_k(c['input_tokens'])} out {_k(c['output_tokens'])} │ "
                 f"🔒 {s.worker_model}: {l['requests']} req in {_k(l['input_tokens'])} out {_k(l['output_tokens'])}"
                 f" (incl. judge {st['judge_requests']}) │ 🌐 search {st['web']['search']} fetch {st['web']['fetch']} │ "
                 f"agents {sum(st['runs'].values())} run: {runs}{active}")
        bar.update(line1 + "\n" + line2)

    # ------------------------------------------------------------ command completion popup
    @property
    def popup_visible(self) -> bool:
        return bool(getattr(self, "_popup_ids", ()))

    @on(TextArea.Changed, "#input")
    def _input_changed(self, ev: TextArea.Changed) -> None:
        self._update_popup()
        self._update_attachments()

    # ------------------------------------------------------------ attachments (paths dropped into the input box)
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
                             deny_names=s.sandbox_deny_names)

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
        if value.startswith("/"):
            self._slash(value)
            return
        self._start_task(value, Force.NONE)

    def _start_task(self, task: str, force: Force) -> None:
        if self._busy:
            self._notice("warn", "The previous task is still running; press Esc to interrupt it before sending.")
            return
        self._update_attachments()
        self._busy = True
        self._refresh_topbar()
        self._timer = self.set_interval(1.0, self._refresh_topbar)
        self.query_one("#plan", Static).update("")
        self.run_worker(self._run_task(task, force), group="task", exclusive=True, exit_on_error=False)

    async def _run_task(self, task: str, force: Force) -> None:
        try:
            m = await asyncio.to_thread(self._load_attachments, task)
            for n in m.notes:
                self._notice("warn", f"Attachment: {n}")
            self._pending_attachments = m.summary
            await self.agent.run(task, m.text, force=force)
        except asyncio.CancelledError:
            self.agent.cancel_running()
            self._notice("warn", "Interrupted. Add instructions to continue.", announce=True)
            raise
        except Exception as e:
            self._notice("error", f"Task failed: {type(e).__name__}: {e}", announce=True)
        finally:
            self._busy = False
            if getattr(self, "_timer", None):
                self._timer.stop()
                self._timer = None
            self._end_stream()
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
            self._notice("warn", f"Voice input unavailable: {e}")
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
            rows = [f"| ☁ DeepSeek (billed) | {c['requests']} | {c['input_tokens']:,} | {c['output_tokens']:,} |",
                    f"| 🔒 Ollama (local, incl. {st['judge_requests']} judge requests) | {l['requests']} | "
                    f"{l['input_tokens']:,} | {l['output_tokens']:,} |"]
            runs = "\n".join(f"- {k}: {v}" for k, v in st["runs"].items()) or "- no agent has run yet"
            self._chat_md("system", "**Usage**\n\n| Model | Requests | Input tokens | Output tokens |\n|---|---|---|---|\n"
                          + "\n".join(rows) + f"\n\n**agent runs** (total {sum(st['runs'].values())})\n\n{runs}"
                          + f"\n\n**Web**: {st['web']['search']} searches, {st['web']['fetch']} fetches"
                          + f"\n\nSession running for {_mmss(st['session_elapsed_s'])}"
                          + (f", current task {_mmss(st['task_elapsed_s'])}" if st["task_elapsed_s"] is not None else "")
                          + (f", waiting for {st['current']}" if st["current"] else ""))
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
        self.exit()

    # ------------------------------------------------------------ approval
    async def _approve(self, req: ApprovalRequest) -> Approval:
        if phrase := phrase_for(req):
            self.speaker.speak(phrase)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.push_screen(ApprovalScreen(req), callback=lambda r: fut.done() or fut.set_result(r))
        return await fut

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
        elif isinstance(ev, ReviewArrived):
            head = f"**🔍 Local review · round {ev.round} · {'passed' if ev.passed else 'failed'}**"
            if not ev.passed and not ev.final:
                head += " (returned to the executor for fixes)"
            body = [head] + [f"- ✗ {x}" for x in ev.problems] + [f"- → {x}" for x in ev.suggestions]
            self._chat_md("report", "\n".join(body))
        elif isinstance(ev, CommandStarted):
            risk = f" [{'red' if ev.risk == 'high' else 'dim'}]risk:{ev.risk}[/]" if ev.risk else ""
            log.write(Text.from_markup(f"[b cyan]$ {escape(ev.tool)}[/] {escape(ev.summary[:300])}{risk}"))
        elif isinstance(ev, CommandFinished):
            color = "green" if ev.exit_code == 0 else "red"
            out = "\n".join(ev.output.splitlines()[:40])
            log.write(Text.from_markup(f"  [{color}]exit={ev.exit_code}[/] {ev.duration_s}s"))
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
            log.write(Text.from_markup(f"[b red]⚑ Session tainted: {escape(ev.reason)}[/]"))
        elif isinstance(ev, SnapshotTaken):
            if ev.label == "task":   # the "Changes" tab and default rollback use the pre-task snapshot; step snapshots are for the reviewer only
                self._last_snapshot = ev.snapshot_id
            log.write(Text.from_markup(f"[dim]📸 Snapshot {ev.snapshot_id} ({ev.kind}, {'pre-task' if ev.label == 'task' else 'pre-step'})[/]"))
        elif isinstance(ev, StatsUpdated):
            self._refresh_topbar()
        elif isinstance(ev, Notice):
            self._notice(ev.level, ev.text)
            self._refresh_topbar()
        elif isinstance(ev, TaskFinished):
            self._end_stream()
            self._notice("info", f"Done · {Route(ev.route).label if ev.route in Route._value2member_map_ else ev.route}"
                                 f" · {ev.backend} · outbound {ev.outbound_count}")
            if self._last_snapshot:
                self._show_diff(self._last_snapshot)

    async def _on_chat(self, ev: ChatMessage) -> None:
        if ev.role == "planner" and ev.streaming:
            if self._stream is None:
                self._stream_md = Markdown("", classes="msg planner")
                self._stream_md.border_title = "☁ Planner (DeepSeek)"
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
        titles = {"executor": "🔒 Local executor", "planner": "☁ Planner (DeepSeek)", "report": "", "system": "ℹ"}
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
        self.query_one(PerfPanel).show(s, self.perf.cpu_history, self.perf.gpu_history)
