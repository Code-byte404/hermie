"""UI tests (Textual Pilot, no real terminal needed)."""
import asyncio

from textual.widgets import Markdown, RichLog, Static

from hermie.config import RunMode
from hermie.perf import PerfSample
from hermie.tui.app import ApprovalScreen, HermieApp, ModelScreen, PerfPanel, VoiceScreen

from .conftest import FakeJudge, Script, final, review, text, tool


async def _submit(pilot, value: str):
    inp = pilot.app.query_one("#input")
    inp.text = value
    inp.focus()
    await pilot.press("enter")


async def _wait_idle(pilot, app, timeout=10):
    for _ in range(int(timeout / 0.05)):
        await pilot.pause(0.05)
        if not app._busy:
            return
    raise TimeoutError


class FakeSampler:
    """Always returns one fixed frame: CPU 42%, GPU 77%, memory 8/32 GB."""

    def __init__(self):
        self.cpu_history, self.gpu_history, self.calls = [], [], 0

    def sample(self):
        self.calls += 1
        s = PerfSample(cpu=42.0, gpu=77.0, mem_used=8 << 30, mem_total=32 << 30, gpu_mem=5 << 30)
        self.cpu_history.append(s.cpu)
        self.gpu_history.append(s.gpu)
        return s


async def test_task_flow_renders_route_chat_and_perf(make_agent):
    agent = make_agent(FakeJudge(task="repetitive"))
    sampler = FakeSampler()
    app = HermieApp(agent=agent, perf=sampler)
    async with app.run_test(size=(160, 45)) as pilot:
        await _submit(pilot, "Translate the title into English")
        await _wait_idle(pilot, app)
        await pilot.pause(0.2)
        assert "local only" in str(app.query_one("#route", Static).render())
        texts = [m.source for m in app.query(Markdown) if hasattr(m, "source")]
        assert any("LOCAL_ANSWER" in t for t in texts)
        app.query_one("#right").active = "tab-perf"
        await pilot.pause(1.2)
        assert sampler.calls >= 1
        panel = app.query_one(PerfPanel)
        assert "42%" in str(panel.query_one("#perf-cpu", Static).render())
        assert "77%" in str(panel.query_one("#perf-gpu", Static).render())
        assert "8.0 / 32.0 GB" in str(panel.query_one("#perf-mem", Static).render())
        graph = str(panel.query_one("#perf-gpu-graph", Static).render())
        assert "█" in graph and graph.count("\n") == 3   # 4-row graph; 77% fills at least one full row


async def test_plan_mode_shows_outbound_and_plan(make_agent):
    planner = Script([tool("set_plan", steps=["generate report", "acceptance"]), tool("delegate", step="generate report report.md"),
                      text("done")], name="planner")
    agent = make_agent(FakeJudge(task="planning"), planner=planner, reviewer=Script([review(True)]), verify_rounds=1)
    app = HermieApp(agent=agent)
    async with app.run_test(size=(160, 45)) as pilot:
        await _submit(pilot, "Plan and generate a report")
        await _wait_idle(pilot, app)
        await pilot.pause(0.2)
        app.query_one("#right").active = "tab-outbound"
        await pilot.pause(0.2)
        assert app.query_one("#outbound", RichLog).lines
        plan = str(app.query_one("#plan", Static).render())
        assert "generate report" in plan and "acceptance" in plan and "delegated" in plan
        assert "outbound 3" in str(app.query_one("#topbar", Static).render())   # task, set_plan return, report
        assert any("Local review · round 1 · passed" in t for _, t in app.transcript)


async def test_approval_modal_deny(make_agent, settings):
    settings.workspace.mkdir(parents=True, exist_ok=True)
    (settings.workspace / "keep.txt").write_text("x")
    ex = Script([tool("run_command", command="rm keep.txt")])
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex, mode=RunMode.DEFAULT)
    app = HermieApp(agent=agent)
    async with app.run_test(size=(160, 45)) as pilot:
        await _submit(pilot, "Clean up")
        for _ in range(100):
            await pilot.pause(0.05)
            if isinstance(app.screen, ApprovalScreen):
                break
        assert isinstance(app.screen, ApprovalScreen)
        await pilot.press("n")
        await _wait_idle(pilot, app)
    assert (settings.workspace / "keep.txt").exists()


async def test_f2_cycles_mode(make_agent):
    agent = make_agent(FakeJudge(), mode=RunMode.DEFAULT)
    app = HermieApp(agent=agent)
    async with app.run_test(size=(160, 45)) as pilot:
        await pilot.press("f2")
        assert agent.s.mode is RunMode.AUTO and app.query_one("#topbar").has_class("mode-auto")
        await pilot.press("f2")
        assert agent.s.mode is RunMode.DEFAULT


async def test_escape_interrupts_running_command(make_agent):
    ex = Script([tool("run_command", command="sleep 30")])
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex)
    app = HermieApp(agent=agent)
    async with app.run_test(size=(160, 45)) as pilot:
        await _submit(pilot, "Wait a moment")
        await pilot.pause(1.0)
        assert app._busy
        await pilot.press("escape")
        await _wait_idle(pilot, app, timeout=5)


async def test_slash_commands(make_agent):
    agent = make_agent(FakeJudge(task="repetitive"))
    app = HermieApp(agent=agent)
    async with app.run_test(size=(160, 45)) as pilot:
        for cmd in ("/help", "/usage", "/snapshots", "/perf", "/outbound", "/mode auto", "/bogus"):
            await _submit(pilot, cmd)
            await pilot.pause(0.05)
        assert agent.s.mode is RunMode.AUTO
        await _submit(pilot, "/local translate")
        await _wait_idle(pilot, app)
        await _submit(pilot, "/export")
        await pilot.pause(0.1)
        assert list((agent.s.data_dir / "exports").glob("*.md"))


async def test_topbar_shows_tokens_agents_and_time(make_agent):
    agent = make_agent(FakeJudge(task="repetitive"))
    app = HermieApp(agent=agent)
    async with app.run_test(size=(180, 45)) as pilot:
        await _submit(pilot, "translate")
        await _wait_idle(pilot, app)
        await pilot.pause(0.2)
        bar = str(app.query_one("#topbar", Static).render())
        assert "agents 1 run: executor×1" in bar and "🔒" in bar and "session" in bar and "current: idle" in bar
        await _submit(pilot, "/usage")
        await pilot.pause(0.1)
        texts = [m.source for m in app.query(Markdown) if hasattr(m, "source")]
        assert any("agent runs" in t and "executor: 1" in t for t in texts)


# ---------------- Voice: F5 records -> transcript goes into the input box; /voice toggle and voice selection; key events are spoken (all fakes)

import numpy as np
from hermie.events import ApprovalRequest


class FakeRecorder:
    def __init__(self):
        self.recording, self.cancelled, self.auto_stopped, self.elapsed_s = False, False, False, 0.0

    def start(self):
        self.recording = True

    def stop(self):
        self.recording = False
        return np.zeros(16000, dtype=np.float32)

    def cancel(self):
        self.recording, self.cancelled = False, True


class FakeTranscriber:
    def __init__(self, text):
        self.text, self.calls = text, 0

    def transcribe(self, audio):
        self.calls += 1
        return self.text


class FakeSpeaker:
    def __init__(self, enabled=False):
        self.enabled, self.voice, self.rate, self.spoken = enabled, "Tingting", 0, []

    def speak(self, text):
        if self.enabled:
            self.spoken.append(text)

    def preview(self, text, voice=None, rate=None):
        self.spoken.append(text)
        self.previewed = (voice, rate)

    def list_voices(self):
        return [("Meijia", "zh_TW"), ("Tingting", "zh_CN")]

    def set_voice(self, name):
        for v, _ in self.list_voices():
            if name.lower() in (v.lower(), {"Meijia": "mei-jia", "Tingting": "ting-ting"}[v]):   # name or nickname
                self.voice = v
                return True
        return False

    def stop(self):
        pass

    def close(self):
        pass


def voice_app(agent, transcript="Convert these three files to PDF", enabled=False):
    rec, tr, sp = FakeRecorder(), FakeTranscriber(transcript), FakeSpeaker(enabled)
    app = HermieApp(agent=agent, recorder=rec, transcriber=tr, speaker=sp)
    return app, rec, tr, sp


async def _wait_transcribed(pilot, app, timeout=5):
    for _ in range(int(timeout / 0.05)):
        await pilot.pause(0.05)
        if not app._transcribing and not app.recorder.recording:
            return


async def test_f5_records_and_fills_input(make_agent):
    app, rec, tr, sp = voice_app(make_agent(FakeJudge(task="repetitive")))
    async with app.run_test(size=(160, 45)) as pilot:
        await pilot.press("f5")
        assert rec.recording and "Recording" in str(app.query_one("#topbar", Static).render())
        await pilot.press("f5")
        await _wait_transcribed(pilot, app)
        assert tr.calls == 1 and app.query_one("#input").text == "Convert these three files to PDF"
        await pilot.press("enter")                       # the transcript goes through the task flow like typed text
        await _wait_idle(pilot, app)
        assert any(t == "Convert these three files to PDF" for r, t in app.transcript if r == "You")


async def test_blank_transcript_is_reported(make_agent):
    app, rec, tr, sp = voice_app(make_agent(FakeJudge()), transcript="...")
    async with app.run_test(size=(160, 45)) as pilot:
        await pilot.press("f5")
        await pilot.press("f5")
        await _wait_transcribed(pilot, app)
        assert app.query_one("#input").text == ""
        assert any("Didn't catch that" in t for _, t in app.transcript) or any(
            "Didn't catch that" in str(w.render()) for w in app.query(".notice"))


async def test_escape_cancels_recording_not_task(make_agent):
    ex = Script([tool("run_command", command="sleep 30")])
    app, rec, tr, sp = voice_app(make_agent(FakeJudge(task="repetitive"), executor=ex))
    async with app.run_test(size=(160, 45)) as pilot:
        await _submit(pilot, "Wait a moment")
        await pilot.pause(1.0)
        await pilot.press("f5")
        await pilot.press("escape")
        assert rec.cancelled and not rec.recording and app._busy   # only the recording is cancelled, the task keeps running
        await pilot.press("escape")
        await _wait_idle(pilot, app, timeout=5)


async def test_voice_toggle_and_task_finished_phrase(make_agent):
    app, rec, tr, sp = voice_app(make_agent(FakeJudge(task="repetitive")))
    async with app.run_test(size=(160, 45)) as pilot:
        assert "🔇" in str(app.query_one("#topbar", Static).render())
        await _submit(pilot, "/voice off")
        await _submit(pilot, "Do something")
        await _wait_idle(pilot, app)
        assert sp.spoken == []
        await pilot.press("f6")
        assert sp.enabled and "🔊" in str(app.query_one("#topbar", Static).render())
        await _submit(pilot, "Do something else")
        await _wait_idle(pilot, app)
        await pilot.pause(0.2)
        assert sp.spoken and sp.spoken[0].startswith("Task done, local only")


async def test_voice_commands(make_agent):
    app, rec, tr, sp = voice_app(make_agent(FakeJudge()), enabled=True)
    async with app.run_test(size=(160, 45)) as pilot:
        await _submit(pilot, "/voice list")
        assert any("Meijia" in t and "Tingting" in t for _, t in app.transcript)
        await _submit(pilot, "/voice mei-jia")
        assert sp.voice == "Meijia" and sp.spoken[-1] == "Switched to Meijia"
        await _submit(pilot, "/voice nosuchvoice")
        assert sp.voice == "Meijia" and any("No voice named" in str(w.render()) for w in app.query(".notice"))
        await _submit(pilot, "/voice test")
        assert "Test" in sp.spoken[-1]
        await _submit(pilot, "/voice")
        await pilot.pause(0.2)
        assert isinstance(app.screen, VoiceScreen)
        await pilot.press("escape")


async def test_approval_is_announced(make_agent, settings):
    settings.workspace.mkdir(parents=True, exist_ok=True)
    (settings.workspace / "keep.txt").write_text("x")
    ex = Script([tool("run_command", command="rm keep.txt")])
    app, rec, tr, sp = voice_app(make_agent(FakeJudge(task="repetitive"), executor=ex, mode=RunMode.DEFAULT),
                                 enabled=True)
    async with app.run_test(size=(160, 45)) as pilot:
        await _submit(pilot, "Clean up")
        for _ in range(100):
            await pilot.pause(0.05)
            if isinstance(app.screen, ApprovalScreen):
                break
        assert sp.spoken and sp.spoken[0].startswith("Approval needed, high-risk command: rm keep.txt")
        await pilot.press("n")
        await _wait_idle(pilot, app)


# ---------------- Command completion popup, /model, /voice key

from textual.widgets import OptionList


async def _type(pilot, text):
    inp = pilot.app.query_one("#input")
    inp.focus()
    inp.text = text
    inp.move_cursor(inp.document.end)
    await pilot.pause(0.1)


async def test_slash_popup_filters_completes_and_executes(make_agent):
    app = HermieApp(agent=make_agent(FakeJudge()))
    async with app.run_test(size=(160, 45)) as pilot:
        popup = app.query_one("#cmd-popup", OptionList)
        assert not popup.display
        await _type(pilot, "/")
        assert popup.display and popup.option_count == len(__import__("hermie.tui.commands", fromlist=["COMMANDS"]).COMMANDS)
        await _type(pilot, "/mo")
        assert [str(popup.get_option_at_index(i).id) for i in range(popup.option_count)] == ["/mode", "/model"]
        await pilot.press("down")
        await pilot.press("tab")
        assert app.query_one("#input").text == "/model " and popup.display     # command with arguments: complete and wait for them
        await _type(pilot, "/hel")
        await pilot.press("enter")                                             # command without arguments: complete and run
        await pilot.pause(0.2)
        assert not popup.display and any("Slash commands" in t for _, t in app.transcript)
        await _type(pilot, "/mo")
        await pilot.press("escape")
        assert not popup.display and not app._busy
        await _type(pilot, "ordinary task")
        assert not popup.display


async def test_model_command_switches_and_writes_env(make_agent, settings):
    agent = make_agent(FakeJudge(task="repetitive"))
    agent.list_local_models = lambda: ["gemma4:12b", "qwen3.8:27b-mlx", "qwen3:8b"]
    app = HermieApp(agent=agent)
    settings.env_path.write_text("# x\nWORKER_MODEL=qwen3.8:27b-mlx\n")
    async with app.run_test(size=(160, 45)) as pilot:
        await _submit(pilot, "/model list")
        assert any("gemma4:12b" in t and "← judge" in t for _, t in app.transcript)
        old_executor = agent.executor
        await _submit(pilot, "/model local qwen3:8b")
        assert agent.s.worker_model == "qwen3:8b" and agent.executor is not old_executor
        await _submit(pilot, "/model cloud deepseek-v4-flash-lite")
        await _submit(pilot, "/model judge nothere:1b")
        await pilot.pause(0.3)
        assert any("nothere:1b is not installed" in str(w.render()) for w in app.query(".notice"))
        await _submit(pilot, "/model bogus x")
        assert any("Usage" in str(w.render()) for w in app.query(".notice"))
        await _submit(pilot, "Do something")
        await _wait_idle(pilot, app)
    env = settings.env_path.read_text()
    assert env.startswith("# x\nWORKER_MODEL=qwen3:8b\n") and "CLOUD_MODEL=deepseek-v4-flash-lite" in env
    assert "JUDGE_MODEL=nothere:1b" in env


async def test_voice_key_command_rebinds_and_writes_env(make_agent, settings):
    app, rec, tr, sp = voice_app(make_agent(FakeJudge()))
    async with app.run_test(size=(160, 45)) as pilot:
        await _submit(pilot, "/voice key enter")
        assert any("must be one of" in str(w.render()) for w in app.query(".notice")) and settings.voice_key == "f5"
        await _submit(pilot, "/voice key f8")
        assert settings.voice_key == "f8" and "F8 record" in str(app.query_one("#hints", Static).render())
        await pilot.press("f5")
        assert not rec.recording
        await pilot.press("f8")
        assert rec.recording
        await pilot.press("f8")
        await _wait_transcribed(pilot, app)
        await _submit(pilot, "/voice on")
        await _submit(pilot, "/voice mei-jia")
    env = settings.env_path.read_text()
    assert "VOICE_KEY=f8" in env and "VOICE_OUTPUT=true" in env and "VOICE_NAME=Meijia" in env


async def test_configured_voice_key_bound_on_start(make_agent, settings):
    settings.voice_key = "f9"
    app, rec, tr, sp = voice_app(make_agent(FakeJudge()))
    async with app.run_test(size=(160, 45)) as pilot:
        await pilot.press("f9")
        assert rec.recording


# ---------------- Config dialogs: /model and /voice open a dialog directly; saving applies immediately and writes .env

from textual.widgets import Input, Label, Select, Switch


async def _wait_screen(pilot, app, cls, timeout=3):
    for _ in range(int(timeout / 0.05)):
        await pilot.pause(0.05)
        if isinstance(app.screen, cls):
            return app.screen
    raise TimeoutError(cls.__name__)


async def test_model_dialog_saves_and_writes_env(make_agent, settings):
    agent = make_agent(FakeJudge())
    agent.list_local_models = lambda: ["gemma4:12b", "qwen3:8b"]
    app = HermieApp(agent=agent)
    async with app.run_test(size=(160, 45)) as pilot:
        await _submit(pilot, "/model")
        scr = await _wait_screen(pilot, app, ModelScreen)
        assert [v for _, v in scr.query_one("#cfg-worker", Select)._options] == ["gemma4:12b", "qwen3.8:27b-mlx", "qwen3:8b"]
        scr.query_one("#cfg-worker", Select).value = "qwen3:8b"
        scr.query_one("#cfg-plan", Input).value = "deepseek-v4-pro-max"
        await pilot.click("#save")
        await pilot.pause(0.2)
        assert not isinstance(app.screen, ModelScreen)
        assert agent.s.worker_model == "qwen3:8b" and agent.s.cloud_plan_model == "deepseek-v4-pro-max"
        env = settings.env_path.read_text()
        assert "WORKER_MODEL=qwen3:8b" in env and "CLOUD_PLAN_MODEL=deepseek-v4-pro-max" in env
        assert "JUDGE_MODEL" not in env                       # unchanged values are not written
        await _submit(pilot, "/model")
        scr = await _wait_screen(pilot, app, ModelScreen)
        scr.query_one("#cfg-cloud", Input).value = ""
        await pilot.click("#save")
        await pilot.pause(0.1)
        assert isinstance(app.screen, ModelScreen) and "cannot be empty" in str(scr.query_one("#config-error").render())
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert not isinstance(app.screen, ModelScreen) and agent.s.cloud_model == "deepseek-v4-flash"


async def test_voice_dialog_saves_preview_and_validates(make_agent, settings):
    app, rec, tr, sp = voice_app(make_agent(FakeJudge()))
    async with app.run_test(size=(160, 45)) as pilot:
        await _submit(pilot, "/voice")
        scr = await _wait_screen(pilot, app, VoiceScreen)
        scr.query_one("#cfg-enabled", Switch).value = True
        scr.query_one("#cfg-voice", Select).value = "Meijia"
        scr.query_one("#cfg-rate", Input).value = "200"
        scr.query_one("#cfg-key", Input).value = "enter"
        await pilot.click("#preview")
        await pilot.pause(0.1)
        assert sp.spoken[-1].startswith("Hello") and sp.previewed == ("Meijia", 200)
        assert not sp.enabled and sp.voice == "Tingting"   # the preview does not change the current settings
        await pilot.click("#save")
        await pilot.pause(0.1)
        assert isinstance(app.screen, VoiceScreen) and "record key" in str(scr.query_one("#config-error").render())
        scr.query_one("#cfg-key", Input).value = "F9"
        await pilot.click("#save")
        await pilot.pause(0.2)
        assert not isinstance(app.screen, VoiceScreen)
        assert sp.enabled and sp.voice == "Meijia" and settings.voice_key == "f9" and settings.voice_rate == 200
        await pilot.press("f9")
        assert rec.recording
        await pilot.press("f9")
        await _wait_transcribed(pilot, app)
    env = settings.env_path.read_text()
    assert all(x in env for x in ("VOICE_OUTPUT=true", "VOICE_NAME=Meijia", "VOICE_RATE=200", "VOICE_KEY=f9"))


async def test_popup_click_opens_dialog_or_completes(make_agent):
    app, rec, tr, sp = voice_app(make_agent(FakeJudge()))
    async with app.run_test(size=(160, 45)) as pilot:
        await _type(pilot, "/vo")
        popup = app.query_one("#cmd-popup", OptionList)
        popup.action_select()                                  # equivalent to clicking the highlighted item
        scr = await _wait_screen(pilot, app, VoiceScreen)
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert app.query_one("#input").text == "" and not popup.display
        await _type(pilot, "/roll")
        popup.action_select()
        await pilot.pause(0.1)
        assert app.query_one("#input").text == "/rollback "  # takes arguments: complete and wait for them
        await _type(pilot, "/hel")
        popup.action_select()
        await pilot.pause(0.2)
        assert any("Slash commands" in t for _, t in app.transcript)


async def test_dropped_path_shows_attachment_strip(make_agent, tmp_path):
    f = tmp_path / "report notes.txt"
    f.write_text("quarterly numbers")
    app = HermieApp(agent=make_agent(FakeJudge()))
    async with app.run_test(size=(160, 45)) as pilot:
        inp = app.query_one("#input")
        strip = app.query_one("#attachments", Static)
        assert not strip.display
        inp.text = "summarize " + str(f).replace(" ", "\\ ")
        await pilot.pause(0.1)
        assert strip.display
        assert "report notes.txt" in str(strip.render()) and "17 B" in str(strip.render())
        inp.text = "summarize"
        await pilot.pause(0.1)
        assert not strip.display


async def test_submitting_with_attachment_feeds_content_to_executor(make_agent, tmp_path):
    f = tmp_path / "data.csv"
    f.write_text("name,amount\nACME,100\n")
    ex = Script(final=final())
    app = HermieApp(agent=make_agent(FakeJudge(task="repetitive"), executor=ex))
    async with app.run_test(size=(160, 45)) as pilot:
        await _submit(pilot, f"total the amounts in {f}")
        await _wait_idle(pilot, app)
        await pilot.pause(0.2)
        assert "ACME,100" in ex.sent_text() and f"[File: {f}]" in ex.sent_text()
        assert any("data.csv" in t for who, t in app.transcript if who == "You")
        assert not app.query_one("#attachments", Static).display


async def test_model_dialog_labels_show_cloud_provider(make_agent):
    agent = make_agent(FakeJudge(), cloud_provider="anthropic")
    agent.list_local_models = lambda: []
    app = HermieApp(agent=agent)
    async with app.run_test(size=(160, 45)) as pilot:
        await _submit(pilot, "/model")
        scr = await _wait_screen(pilot, app, ModelScreen)
        labels = [str(lbl.render()) for lbl in scr.query(Label)]
        assert any("Anthropic" in lbl for lbl in labels) and not any("DeepSeek" in lbl for lbl in labels)


async def test_calibrate_command_shows_report(make_agent, settings):
    app = HermieApp(agent=make_agent(FakeJudge()))
    async with app.run_test(size=(160, 45)) as pilot:
        await _submit(pilot, "/calibrate")
        await pilot.pause(0.2)
        assert any("Routing calibration" in t for who, t in app.transcript)


async def test_skills_command_lists_and_changes_status(make_agent, settings):
    agent = make_agent(FakeJudge(), skills_enabled=True)
    body = "## When to use\ncsv\n\n## Steps\n1. csv\n\n## Verify\n- csv"
    sk, _ = agent.session.skills.add_candidate("Convert a spreadsheet to csv", body, workspace="w", task_type="x")
    app = HermieApp(agent=agent)
    async with app.run_test(size=(160, 45)) as pilot:
        await _submit(pilot, "/skills")
        await pilot.pause(0.2)
        assert any("Convert a spreadsheet to csv" in t and "candidate" in t for who, t in app.transcript)
        await _submit(pilot, f"/skills approve {sk.id[:6]}")
        await pilot.pause(0.1)
        assert agent.session.skills.all()[0].status == "active"
        await _submit(pilot, "/skills retire nosuchid")
        await pilot.pause(0.1)
        assert agent.session.skills.all()[0].status == "active"
        await _submit(pilot, f"/skills retire {sk.id}")
        await pilot.pause(0.1)
        assert agent.session.skills.all()[0].status == "retired"
