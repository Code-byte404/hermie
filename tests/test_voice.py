"""Voice module: spoken phrases (pure function), the Speaker queue (fake subprocess), Recorder / Transcriber
(injected fake audio stream and fake recognition backend).
Needs no microphone, model or sound card; sounddevice / mlx_whisper must never be imported."""
import sys
import threading

import numpy as np
import pytest

from hermie.config import Settings
from hermie.events import ApprovalRequest, InputRequest, Notice, PlanUpdated, TaskFinished
from hermie.voice import (Recorder, Speaker, Transcriber, VoiceUnavailable, first_sentence, parse_voices,
                                phrase_for)


# ---------------- phrases

def test_first_sentence():
    assert first_sentence("Finished. Two files produced") == "Finished"
    assert first_sentence("Done. Next") == "Done"
    assert first_sentence("Really? Yes!") == "Really"
    assert first_sentence("first line\nsecond line") == "first line"
    assert first_sentence("Done\u3002Next") == "Done"        # leftover CJK full stop still splits
    assert first_sentence("x" * 80) == "x" * 60 + "..."
    assert first_sentence("") == ""


def test_task_finished_phrase():
    ev = TaskFinished("local", "ollama", "Report generated. Files produced:\n- a.csv\n- b.csv", 0)
    assert phrase_for(ev) == "Task done, local only, 2 files produced. Report generated"
    assert phrase_for(TaskFinished("cloud", "deepseek", "The plan is as follows: step one", 1)) == \
        "Task done, cloud direct. The plan is as follows: step one"
    assert phrase_for(TaskFinished("plan", "deepseek-plan+ollama", "", 2)) == "Task done, plan mode."


def test_approval_phrases():
    assert phrase_for(ApprovalRequest("run_command", "rm -rf build", "high", "high-risk command")) == \
        "Approval needed, high-risk command: rm -rf build"
    assert phrase_for(ApprovalRequest("web_fetch", "https://news.example.com/a?x=1", "medium", "...")) == \
        "Approval needed, web request: news.example.com"
    assert phrase_for(ApprovalRequest("web_search", "industry news", "medium", "...")) == \
        "Approval needed, search request: industry news"
    assert phrase_for(InputRequest("npx create-next-app web", "Ok to proceed? (y)")) == \
        "A command is waiting for your input: npx create-next-app web"


def test_notice_phrases():
    assert phrase_for(Notice("error", "Task failed: RuntimeError: x")) == "Task failed: RuntimeError: x"
    assert phrase_for(Notice("warn", "DeepSeek unavailable, falling back to local: timeout")) == \
        "DeepSeek unavailable, falling back to local: timeout"
    assert phrase_for(Notice("warn", "Interrupted. Add instructions to continue.")) == \
        "Interrupted. Add instructions to continue."
    assert phrase_for(Notice("warn", "Snapshot failed; rollback unavailable for this task")) is None
    assert phrase_for(Notice("info", "done")) is None
    assert phrase_for(PlanUpdated([], [])) is None
    assert phrase_for(Notice("error", "x" * 100)) == "x" * 60 + "..."


# ---------------- Speaker

class FakeProc:
    """say -o synthesis step: writes the file immediately and exits; afplay playback step: blocks until the
    test releases it (done.set)."""

    def __init__(self, argv):
        self.argv, self.done, self.killed = argv, threading.Event(), False
        if argv[0].endswith("say") and "-o" in argv:
            with open(argv[argv.index("-o") + 1], "wb") as f:
                f.write(b"AIFF")
            self.done.set()

    def wait(self, timeout=None):
        self.done.wait(timeout)
        return 0

    def kill(self):
        self.killed = True
        self.done.set()

    def poll(self):
        return 0 if self.done.is_set() else None


VOICES = """Meijia               zh_TW    # Hello, my name is Mei-Jia.
Tingting             zh_CN    # Hello, my name is Ting-Ting.
Samantha             en_US    # Hello, my name is Samantha.
Sinji                zh_HK    # Hello, my name is Sin-Ji.
"""


@pytest.fixture
def speaker():
    procs = []

    def spawn(argv):
        p = FakeProc(argv)
        procs.append(p)
        return p
    sp = Speaker(Settings(voice_output=True, voice_name="Tingting", voice_rate=180), spawn=spawn, voices_output=VOICES)
    sp.procs = procs
    yield sp
    sp.close()


def _wait_for(cond, timeout=2.0):
    t = threading.Event()
    for _ in range(int(timeout / 0.01)):
        if cond():
            return True
        t.wait(0.01)
    return False


def says(procs):
    return [p for p in procs if p.argv[0].endswith("say")]


def plays(procs):
    return [p for p in procs if p.argv[0].endswith("afplay")]


def test_speaker_synthesizes_to_file_then_plays_in_order(speaker):
    speaker.speak("first sentence")
    speaker.speak("second sentence")
    assert _wait_for(lambda: len(plays(speaker.procs)) == 1)
    say1, play1 = says(speaker.procs)[0], plays(speaker.procs)[0]
    assert say1.argv[:5] == ["/usr/bin/say", "-v", "Tingting", "-r", "180"] and say1.argv[5] == "-o"
    assert say1.argv[-1] == "first sentence" and say1.argv[6].endswith(".aiff") and play1.argv == ["/usr/bin/afplay", say1.argv[6]]
    assert len(says(speaker.procs)) == 1              # the second sentence is synthesized only after the first finished playing
    play1.done.set()
    assert _wait_for(lambda: len(plays(speaker.procs)) == 2)
    assert says(speaker.procs)[1].argv[-1] == "second sentence"
    assert _wait_for(lambda: not __import__("os").path.exists(say1.argv[6]))   # temp file removed


def test_speaker_disabled_does_not_spawn_and_stops_current(speaker):
    speaker.speak("a very long sentence")
    assert _wait_for(lambda: len(plays(speaker.procs)) == 1)
    speaker.speak("a queued sentence")
    speaker.enabled = False
    assert plays(speaker.procs)[0].killed
    speaker.speak("a sentence after switching off")
    assert not _wait_for(lambda: len(says(speaker.procs)) > 1, timeout=0.3)   # queue cleared, nothing new spawned
    speaker.enabled = True
    speaker.speak("switched back on")
    assert _wait_for(lambda: len(plays(speaker.procs)) == 2) and says(speaker.procs)[1].argv[-1] == "switched back on"


def test_speaker_voice_selection(speaker):
    assert parse_voices(VOICES) == [("Meijia", "zh_TW"), ("Tingting", "zh_CN"), ("Sinji", "zh_HK")]
    assert speaker.list_voices() == [("Meijia", "zh_TW"), ("Tingting", "zh_CN"), ("Sinji", "zh_HK")]
    assert speaker.set_voice("mei-jia") and speaker.voice == "Meijia"     # alias from the self-introduction
    assert speaker.set_voice("sinji") and speaker.voice == "Sinji"
    assert not speaker.set_voice("nonexistent") and speaker.voice == "Sinji"
    speaker.speak("Test")
    assert _wait_for(lambda: len(says(speaker.procs)) == 1) and says(speaker.procs)[0].argv[2] == "Sinji"


def test_speaker_without_voice_and_rate_omits_flags():
    procs = []
    sp = Speaker(Settings(voice_output=True, voice_name="", voice_rate=0), spawn=lambda a: procs.append(FakeProc(a)) or procs[-1])
    sp.speak("hi")
    assert _wait_for(lambda: len(says(procs)) == 1)
    assert says(procs)[0].argv[0] == "/usr/bin/say" and says(procs)[0].argv[1] == "-o" and says(procs)[0].argv[-1] == "hi"
    sp.close()


# ---------------- Recorder / Transcriber

class FakeStream:
    def __init__(self, callback):
        self.callback, self.started, self.closed = callback, False, False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def close(self):
        self.closed = True


def test_recorder_collects_chunks_and_auto_stops():
    streams = []

    def factory(callback):
        s = FakeStream(callback)
        streams.append(s)
        return s
    rec = Recorder(max_seconds=1, sample_rate=100, stream_factory=factory)
    assert not rec.recording
    rec.start()
    assert rec.recording and streams[0].started
    streams[0].callback(np.ones((40, 1), dtype=np.float32))
    streams[0].callback(np.zeros((40, 1), dtype=np.float32))
    assert rec.elapsed_s == pytest.approx(0.8) and not rec.auto_stopped
    streams[0].callback(np.ones((40, 1), dtype=np.float32))
    assert rec.auto_stopped
    audio = rec.stop()
    assert audio.shape == (120,) and audio.dtype == np.float32 and audio[0] == 1 and audio[50] == 0
    assert not rec.recording and streams[0].closed


def test_recorder_cancel_discards():
    rec = Recorder(max_seconds=5, sample_rate=100, stream_factory=lambda cb: FakeStream(cb))
    rec.start()
    rec._stream.callback(np.ones((10, 1), dtype=np.float32))
    rec.cancel()
    assert not rec.recording and rec.stop().shape == (0,)


def test_recorder_without_sounddevice_raises(monkeypatch):
    monkeypatch.setitem(sys.modules, "sounddevice", None)   # simulate "not installed": import raises ImportError
    with pytest.raises(VoiceUnavailable, match="sounddevice"):
        Recorder(max_seconds=5).start()


def test_transcriber_with_backend_and_without_mlx(monkeypatch):
    t = Transcriber("m", backend=lambda audio, model: {"text": " hello, world "})
    assert t.transcribe(np.zeros(10, dtype=np.float32)) == "hello, world"
    monkeypatch.setitem(sys.modules, "mlx_whisper", None)
    with pytest.raises(VoiceUnavailable, match="mlx-whisper"):
        Transcriber("m").transcribe(np.zeros(10, dtype=np.float32))


def test_heavy_modules_not_imported():
    """The real sounddevice / mlx_whisper are never imported in the test process (lazy imports + fakes)."""
    assert sys.modules.get("sounddevice") is None and sys.modules.get("mlx_whisper") is None


def test_prime_tqdm_lock_under_textual_like_stderr(monkeypatch):
    """Once Textual replaces stderr with an object whose fileno() returns -1, creating tqdm's lock fails with
    bad value(s) in fds_to_keep; prime must work around it."""
    from tqdm.std import TqdmDefaultWriteLock, tqdm
    from hermie.voice import prime_tqdm_lock

    class Cap:
        def write(self, t): pass
        def flush(self): pass
        def isatty(self): return True
        def fileno(self): return -1

    monkeypatch.delattr(TqdmDefaultWriteLock, "mp_lock", raising=False)
    monkeypatch.delattr(tqdm, "_lock", raising=False)
    monkeypatch.setattr(sys, "stderr", Cap())
    monkeypatch.setattr(sys, "stdout", Cap())
    prime_tqdm_lock()
    assert hasattr(TqdmDefaultWriteLock, "mp_lock") and hasattr(tqdm, "_lock")
    tqdm(total=1, disable=True).close()      # later instances no longer fail
    assert isinstance(sys.stderr, Cap)         # swapping the real stderr back in was only temporary


def test_preview_plays_even_when_disabled_with_given_voice():
    procs = []
    sp = Speaker(Settings(voice_output=False, voice_name="Tingting"), spawn=lambda a: procs.append(FakeProc(a)) or procs[-1],
                 voices_output=VOICES)
    sp.speak("ignored")                                  # speech is off: not spoken
    sp.preview("Test", "Meijia", 200)
    assert _wait_for(lambda: len(plays(procs)) == 1)
    assert says(procs)[0].argv[:5] == ["/usr/bin/say", "-v", "Meijia", "-r", "200"] and says(procs)[0].argv[-1] == "Test"
    assert sp.voice == "Tingting" and not sp.enabled   # preview does not change settings
    plays(procs)[0].done.set()
    sp.preview("default voice")
    assert _wait_for(lambda: len(plays(procs)) == 2)
    assert says(procs)[1].argv[:3] == ["/usr/bin/say", "-v", "Tingting"] and says(procs)[1].argv[-1] == "default voice"
    sp.close()


def test_planner_requests_are_spoken():
    from hermie.events import ClarifyRequest, PlanReviewRequest, QuestionView
    assert phrase_for(ClarifyRequest(1, [QuestionView("q", ["a", "b"])])) == "The planner has questions"
    assert phrase_for(PlanReviewRequest({}, "", 1, {})) == "Plan ready for review"


def test_choice_request_is_spoken():
    from hermie.events import ChoiceRequest
    from hermie.voice import phrase_for
    assert phrase_for(ChoiceRequest("Which app?", ["A"])) == "Hermie needs you to pick an option"
