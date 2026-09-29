"""Voice input and spoken announcements of key events. Everything runs on this machine: recording stays in
memory, recognition uses local Whisper, synthesis uses macOS `say`.

The core layer knows nothing about voice; the UI maps hotkeys to recording and events to announcements.
All three units accept fake implementations for tests:
- Recorder: sounddevice, 16 kHz mono;
- Transcriber: mlx-whisper, lazily loaded;
- Speaker: a queue of `say` subprocesses, can be switched on/off, voice selectable;
- phrase_for: pure function, event -> spoken phrase (only task finished, approval, errors).
"""
from __future__ import annotations

import logging
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
from typing import Callable, Optional
from urllib.parse import urlparse

import numpy as np

from .config import Settings
from .events import ApprovalRequest, Event, InputRequest, Notice, TaskFinished
from .policy import Route

log = logging.getLogger(__name__)

SAY = "/usr/bin/say"
AFPLAY = "/usr/bin/afplay"


class VoiceUnavailable(RuntimeError):
    """Voice feature unavailable (missing dependency, no microphone, model failed to load).
    Only affects voice, never typing."""


# ====================================================================== phrases

# English sentence end (. ! ? newline) plus any leftover CJK punctuation (U+3002, U+FF01, U+FF1F).
_SENTENCE_END = re.compile(r"[\u3002\uff01\uff1f!?.\n]")


def first_sentence(text: str, limit: int = 60) -> str:
    text = text.strip()
    if not text:
        return ""
    m = _SENTENCE_END.search(text)
    if m:
        text = text[: m.start()].strip()
    return text if len(text) <= limit else text[:limit] + "..."


ARTIFACTS_MARKER = "Files produced:"   # emitted by core._local_output before the artifact list


def _artifact_count(output: str) -> int:
    _, sep, tail = output.partition(ARTIFACTS_MARKER)
    if not sep:
        return 0
    return sum(1 for line in tail.splitlines() if line.strip().startswith("- "))


def phrase_for(ev: Event | ApprovalRequest | InputRequest) -> Optional[str]:
    """Whether to speak and what to say. Only four kinds are spoken: task finished (plus a one-sentence
    summary), approval needed, a command waiting for input, error/fallback/interruption. Everything else
    returns None."""
    if isinstance(ev, TaskFinished):
        label = Route(ev.route).label if ev.route in Route._value2member_map_ else ev.route
        n = _artifact_count(ev.output)
        head = f"Task done, {label}" + (f", {n} files produced" if n else "") + "."
        summary = first_sentence(ev.output.split(ARTIFACTS_MARKER)[0])
        return head + (" " + summary if summary else "")
    if isinstance(ev, ApprovalRequest):
        if ev.tool == "web_fetch":
            return f"Approval needed, web request: {urlparse(ev.summary).hostname or ''}"
        if ev.tool == "web_search":
            return f"Approval needed, search request: {ev.summary[:40]}"
        return f"Approval needed, {ev.risk}-risk command: {ev.summary[:40]}"
    if isinstance(ev, InputRequest):
        return f"A command is waiting for your input: {ev.command[:40]}"
    if isinstance(ev, Notice):
        if ev.level == "error" or (ev.level == "warn" and ("falling back" in ev.text or "Interrupted" in ev.text)):
            return ev.text if len(ev.text) <= 60 else ev.text[:60] + "..."
    return None


# ====================================================================== speaking

def parse_voices(text: str) -> list[tuple[str, str]]:
    """Parse the output of `say -v ?`, keeping only Chinese voices: [(name, language)]."""
    out = []
    for line in text.splitlines():
        m = re.match(r"^(\S.*?)\s+(zh_[A-Z]{2})\s", line)
        if m:
            out.append((m.group(1).strip(), m.group(2)))
    return out


class Speaker:
    """Background thread speaks items one by one; disabling kills the current item and clears the queue.

    We do not let `say` synthesize and play at the same time: it synthesizes in real time, and as soon as
    the UI process, Ollama or whisper get busy it stutters. Instead `say -o` renders the whole sentence to
    a temp file first and `afplay` plays it back: file playback is buffered and survives CPU contention."""

    def __init__(self, settings: Settings, *, spawn: Optional[Callable[[list[str]], object]] = None,
                 voices_output: Optional[str] = None):
        self.s = settings
        self._enabled = settings.voice_output
        self.voice = settings.voice_name
        self.rate = settings.voice_rate
        self._spawn = spawn or (lambda argv: subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                                                              stderr=subprocess.DEVNULL))
        self._voices_output = voices_output
        self._queue: queue.Queue = queue.Queue()
        self._current = None
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="speaker")
        self._thread.start()
        if voices_output is None:  # say -v ? takes ~0.6 s: prefetch in the background so the config dialog opens instantly
            threading.Thread(target=self.list_voices, daemon=True, name="voices").start()

    # ---- switch
    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = bool(value)
        if not value:
            self.stop()

    # ---- speaking
    def speak(self, text: str) -> None:
        if self._enabled and text and text.strip():
            self._queue.put((text.strip(), None, None, False))

    def preview(self, text: str, voice: Optional[str] = None, rate: Optional[int] = None) -> None:
        """Preview: ignores the on/off switch, speaks one sentence with the given voice and rate
        (current settings when not given)."""
        if text and text.strip():
            self._queue.put((text.strip(), voice, rate, True))

    def stop(self) -> None:
        """Kill the item being spoken and clear the queue."""
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        with self._lock:
            if self._current is not None:
                try:
                    self._current.kill()
                except Exception:
                    pass
                self._current = None

    def _argv(self, text: str, voice: Optional[str] = None, rate: Optional[int] = None,
              out: Optional[str] = None) -> list[str]:
        argv = [SAY]
        voice = self.voice if voice is None else voice
        rate = self.rate if rate is None else rate
        if voice:
            argv += ["-v", voice]
        if rate:
            argv += ["-r", str(int(rate))]
        if out:
            argv += ["-o", out]
        return argv + [text]

    def _run(self, argv: list[str]) -> int:
        """Spawn a subprocess and wait for it; stop() may kill it midway. Returns the exit code."""
        with self._lock:
            self._current = self._spawn(argv)
        try:
            return self._current.wait() or 0
        finally:
            with self._lock:
                self._current = None

    def _loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            text, voice, rate, force = item
            if not self._enabled and not force:
                continue
            path = None
            try:
                fd, path = tempfile.mkstemp(prefix="hybrid-say-", suffix=".aiff")
                os.close(fd)
                if self._run(self._argv(text, voice, rate, out=path)) == 0 and os.path.getsize(path) > 0:
                    self._run([AFPLAY, path])
            except Exception:
                log.exception("Speech playback failed")
            finally:
                if path:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass

    def close(self) -> None:
        self.stop()
        self._queue.put(None)

    # ---- voices
    def list_voices(self) -> list[tuple[str, str]]:
        if self._voices_output is None:
            try:
                self._voices_output = subprocess.run([SAY, "-v", "?"], capture_output=True, text=True,
                                                     timeout=10).stdout
            except (OSError, subprocess.SubprocessError):
                self._voices_output = ""
        return parse_voices(self._voices_output)

    def set_voice(self, name: str) -> bool:
        """Pick a voice by name or by the self-introduction shown in `say -v ?` (an alias such as the voice's
        own spelling of its name); returns False and keeps the current voice when nothing matches."""
        want = name.strip().lower()
        if not want:
            return False
        for vname, _lang in self.list_voices():
            if vname.lower() == want:
                self.voice = vname
                return True
        for line in (self._voices_output or "").splitlines():
            if want in line.lower():
                m = re.match(r"^(\S.*?)\s+zh_", line)
                if m:
                    self.voice = m.group(1).strip()
                    return True
        return False


# ====================================================================== recording

class Recorder:
    """Hotkey-toggled recording; audio chunks accumulate in memory, stop() returns a float32 mono array.
    Past the limit, auto_stopped is set and the UI performs the stop."""

    def __init__(self, max_seconds: float, sample_rate: int = 16000,
                 stream_factory: Optional[Callable[[Callable], object]] = None):
        self.max_seconds, self.sample_rate = max_seconds, sample_rate
        self._factory = stream_factory or self._sounddevice_stream
        self._stream = None
        self._chunks: list[np.ndarray] = []
        self._frames = 0
        self.recording = False
        self.auto_stopped = False

    def _sounddevice_stream(self, callback):
        try:
            import sounddevice as sd
        except ImportError as e:
            raise VoiceUnavailable("sounddevice is not installed: pip install sounddevice") from e

        def cb(indata, frames, time, status):
            callback(indata)
        try:
            sd.query_devices(kind="input")
        except Exception as e:  # no input device at all (e.g. a Mac mini with no microphone attached)
            raise VoiceUnavailable("No microphone detected; connect a microphone or headset and retry") from e
        try:
            return sd.InputStream(samplerate=self.sample_rate, channels=1, dtype="float32", callback=cb)
        except Exception as e:  # permission denied etc.
            raise VoiceUnavailable(f"Cannot open microphone: {e} (on first use, allow the terminal to access "
                                   "the microphone in System Settings)") from e

    def _on_chunk(self, indata) -> None:
        if not self.recording:
            return
        self._chunks.append(np.array(indata, dtype=np.float32).reshape(-1))
        self._frames += len(indata)
        if self._frames >= self.max_seconds * self.sample_rate:
            self.auto_stopped = True

    @property
    def elapsed_s(self) -> float:
        return self._frames / self.sample_rate

    def start(self) -> None:
        self._chunks, self._frames, self.auto_stopped = [], 0, False
        self._stream = self._factory(self._on_chunk)
        self.recording = True
        self._stream.start()

    def _close(self) -> None:
        self.recording = False
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def stop(self) -> np.ndarray:
        self._close()
        audio = np.concatenate(self._chunks) if self._chunks else np.zeros(0, dtype=np.float32)
        self._chunks = []
        return audio

    def cancel(self) -> None:
        self._close()
        self._chunks, self._frames = [], 0


# ====================================================================== recognition

def prime_tqdm_lock() -> None:
    """Create tqdm's global lock ahead of time.

    mlx-whisper uses tqdm internally; the first tqdm instance creates a multiprocessing.RLock, which starts
    the resource_tracker subprocess and passes it sys.stderr.fileno(). Under Textual, stderr is a capture
    object whose fileno() returns -1, so that fails with "bad value(s) in fds_to_keep". So build the lock
    before the UI starts (real stderr); if already inside the UI, temporarily swap the real stderr back in
    (the window is tiny and the lock is built only once)."""
    try:
        from tqdm.std import TqdmDefaultWriteLock, tqdm
    except ImportError:
        return
    if hasattr(TqdmDefaultWriteLock, "mp_lock") and hasattr(tqdm, "_lock"):
        return
    try:
        tqdm.get_lock()
        return
    except ValueError:
        pass
    saved_err, saved_out = sys.stderr, sys.stdout
    try:
        sys.stderr, sys.stdout = sys.__stderr__, sys.__stdout__
        tqdm.get_lock()
    except Exception:
        log.exception("tqdm lock initialization failed")
    finally:
        sys.stderr, sys.stdout = saved_err, saved_out


class Transcriber:
    """Local Whisper (mlx-whisper). Synchronous and blocking; the UI calls it from a thread."""

    def __init__(self, model: str, *, backend: Optional[Callable] = None):
        self.model = model
        self._backend = backend

    def _mlx(self, audio: np.ndarray, model: str) -> dict:
        try:
            import mlx_whisper
        except ImportError as e:
            raise VoiceUnavailable("mlx-whisper is not installed: pip install mlx-whisper") from e
        prime_tqdm_lock()
        return mlx_whisper.transcribe(audio, path_or_hf_repo=model, language="zh")

    def transcribe(self, audio: np.ndarray) -> str:
        if audio.size == 0:
            return ""
        backend = self._backend or self._mlx
        try:
            result = backend(audio, self.model)
        except VoiceUnavailable:
            raise
        except Exception as e:
            log.exception("Speech recognition failed")
            raise VoiceUnavailable(f"Speech recognition failed: {type(e).__name__}: {e}") from e
        return (result.get("text") if isinstance(result, dict) else str(result)).strip()


def is_blank_transcript(text: str) -> bool:
    # \w plus the CJK Unified Ideographs block (U+4E00-U+9FFF)
    return not re.search(r"[\w\u4e00-\u9fff]", text or "")
