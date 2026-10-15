"""Approvals for held / withheld content: allow store, session switch, terminal prompt."""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

KEEP = 500
_FULL_HASH = re.compile(r"[0-9a-f]{64}")
Choice = Literal["send", "reject", "allow_all", "local"]
_KEYS: dict[str, Choice] = {"s": "send", "r": "reject", "a": "allow_all"}


@dataclass
class PendingItem:
    hash: str
    kind: str
    reason: str
    size: int
    excerpt: str
    id: str | None = None
    local_ok: bool = False


def _append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps(row) + "\n")
    os.chmod(path, 0o600)


def _read_rows(path: Path) -> list[dict]:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    rows = []
    for ln in lines:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if isinstance(r, dict):
            rows.append(r)
    return rows


class AllowStore:
    """Pending (held / withheld) items by short id, and the released content hashes.

    Ids are hash prefixes (4 hex chars, longer on a collision) handed out by `assign`, the one id source for the walker,
    the receipt and the CLI; pending.jsonl keeps each id so another process resolves the id the note showed.
    allowed.jsonl only ever holds full hashes."""

    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir)
        self.allowed_path = self.dir / "allowed.jsonl"
        self.pending_path = self.dir / "pending.jsonl"
        self._ids: set[str] = set()
        self._mtime: int | None = None
        self._lock = threading.RLock()
        self._persisted: set[str] = set()
        self._issued: dict[str, str] = {}   # id -> hash for every id written to pending.jsonl: never given to another
        self.pending: dict[str, PendingItem] = self._load_pending()

    def _load_pending(self) -> dict[str, PendingItem]:
        try:
            nlines = len(self.pending_path.read_text().splitlines())
        except OSError:
            nlines = 0
        rows = _read_rows(self.pending_path)
        kept = rows[-KEEP:]
        if nlines > 2 * KEEP:
            self.pending_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.pending_path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                for r in kept:
                    f.write(json.dumps(r) + "\n")
            os.replace(tmp, self.pending_path)
        # newest first, so an id written twice (an older file) names the item its latest note showed; then back in
        # file order, which `_trim` relies on
        placed: dict[str, tuple[int, PendingItem]] = {}
        table: dict[str, PendingItem] = {}
        for n, r in reversed(list(enumerate(kept))):
            try:
                item = PendingItem(r["hash"], r["kind"], r["reason"], int(r["size"]), "")
            except (KeyError, TypeError, ValueError):
                continue
            if item.hash in self._persisted:
                continue
            want = r.get("id")
            key = self._place(table, item, want if isinstance(want, str) else None)
            placed[key] = (n, item)
            self._issued[key] = item.hash
            self._persisted.add(item.hash)
        return {k: item for k, (_, item) in sorted(placed.items(), key=lambda kv: kv[1][0])}

    def _place(self, table: dict[str, PendingItem], item: PendingItem, want: str | None = None) -> str:
        for k, v in table.items():
            if v.hash == item.hash:
                table[k] = item
                item.id = k
                return k

        def free(k: str) -> bool:
            return k not in table and self._issued.get(k, item.hash) == item.hash

        if want and len(want) >= 4 and item.hash.startswith(want) and free(want):
            key = want
        else:
            key = next((item.hash[:n] for n in range(4, len(item.hash) + 1, 2) if free(item.hash[:n])), item.hash)
        table[key] = item
        item.id = key
        return key

    def _trim(self) -> None:
        while len(self.pending) > KEEP:
            self.pending.pop(next(iter(self.pending)))

    def assign(self, h: str) -> str:
        """The id for this content hash: the existing one, or a new prefix that names no other pending item.
        Idempotent; the item is written to pending.jsonl by `register`."""
        with self._lock:
            for k, v in self.pending.items():
                if v.hash == h:
                    return k
            key = self._place(self.pending, PendingItem(h, "", "", 0, ""))
            self._trim()
            return key

    def register(self, item: PendingItem, again: bool = False) -> str:
        """Record a held / withheld item under its id; written to pending.jsonl once (or `again`)."""
        with self._lock:
            key = self._place(self.pending, item)
            self._trim()
            if again or item.hash not in self._persisted:
                self._issued[key] = item.hash
                _append(self.pending_path, {"hash": item.hash, "id": key, "kind": item.kind, "reason": item.reason,
                                            "size": item.size, "at": _now()})
                self._persisted.add(item.hash)
            return key

    def _refresh(self) -> None:
        try:
            m = self.allowed_path.stat().st_mtime_ns
        except OSError:
            self._ids, self._mtime = set(), None
            return
        if m == self._mtime:
            return
        self._ids = {str(r["id"]) for r in _read_rows(self.allowed_path) if r.get("id")}
        self._mtime = m

    def is_allowed(self, h: str) -> bool:
        """Full hashes only: an id is never a prefix wildcard."""
        self._refresh()
        return h in self._ids

    def allow(self, id_or_hash: str) -> bool:
        """Release a pending id (or a full content hash). An unknown id, or an item that could not be scanned, is
        refused (False) and nothing is written."""
        item = self.pending.get(id_or_hash)
        if item is not None and not releasable(item.reason):
            return False
        if item is not None:
            h = item.hash
        elif _FULL_HASH.fullmatch(id_or_hash):
            h = id_or_hash
        else:
            return False
        _append(self.allowed_path, {"id": h, "at": _now()})
        return True


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def session_releasable(reason: str) -> bool:
    """`[a]llow everything this session` covers judge decisions only (and the smuggling check that stands in for
    the judge), never a detector error or an image."""
    return reason == "judge" or reason.startswith("smuggling:")


def releasable(reason: str) -> bool:
    """A detector error is never released: its text could not be scanned, so it has no pattern-redacted form."""
    return not reason.startswith("detector_error")


class SessionAllow:
    def __init__(self) -> None:
        self.all = False

    def is_allowed(self, h: str, reason: str = "") -> bool:
        return self.all and session_releasable(reason)


class Approvals:
    def __init__(self, store: AllowStore, session: SessionAllow):
        self.store = store
        self.session = session
        self.dropped: set[str] = set()   # messages the user rejected; Claude Code resends them in its history
        self.rewrites: dict[str, str] = {}   # content hash -> rewritten text (already scanned), memory only

    def assign(self, h: str) -> str:
        return self.store.assign(h)

    def is_allowed(self, h: str, reason: str = "") -> bool:
        if not releasable(reason):
            return False
        return self.session.is_allowed(h, reason) or self.store.is_allowed(h)


class NoPrompter:
    available = False

    async def ask(self, item: PendingItem) -> Choice:
        return "reject"


class TtyPrompter:
    def __init__(self, timeout_s: float, stdin=None, stdout=None):
        self.timeout_s = timeout_s
        injected = stdin is not None or stdout is not None
        self.stdin = stdin if stdin is not None else sys.stdin
        self.stdout = stdout if stdout is not None else sys.stdout
        self.available = True if injected else sys.stdin.isatty()
        self._lock = asyncio.Lock()
        self._queue: asyncio.Queue | None = None
        self._reader: threading.Thread | None = None
        self._eof = False
        self._ticker: asyncio.Task | None = None

    def _say(self, s: str) -> None:
        self.stdout.write(s)
        self.stdout.flush()

    def _deliver(self, line: str | None) -> None:
        if line is None:
            self._eof = True
        self._queue.put_nowait(line)

    def _read_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        while True:
            try:
                line = self.stdin.readline()
            except (OSError, ValueError):
                line = ""
            try:
                loop.call_soon_threadsafe(self._deliver, line if line else None)
            except RuntimeError:  # loop closed
                return
            if not line:
                return

    def _start_reader(self) -> None:
        if self._reader is None:
            loop = asyncio.get_running_loop()
            self._queue = asyncio.Queue()
            self._reader = threading.Thread(target=self._read_loop, args=(loop,), daemon=True,
                                            name="hermie-prompt-reader")
            self._reader.start()

    async def _countdown(self, deadline: float) -> None:
        loop = asyncio.get_running_loop()
        while True:
            left = max(0, int(deadline - loop.time() + 0.99))
            self._say(f"\r  waiting {left}s ... ")
            await asyncio.sleep(1)

    async def ask(self, item: PendingItem) -> Choice:
        contended = self._lock.locked()
        async with self._lock:
            self._start_reader()
            if not contended:  # keyboard input typed while no prompt was open is discarded
                while not self._queue.empty():
                    self._queue.get_nowait()
            if self._eof and self._queue.empty():
                return "reject"
            return await self._ask(item)

    async def _ask(self, item: PendingItem) -> Choice:
        excerpt = " ".join(item.excerpt[:200].split())
        self._say(f"\nHermie holds this {item.kind} ({item.size} chars): {excerpt}\n"
                  f"  reason: {item.reason}\n"
                  f"  kind: {item.kind}\n"
                  "  [s]end as is  [r]eject  [a]llow everything this session\n"
                  f"  or later: hermie allow {item.id or item.hash[:4]}\n")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout_s
        self._ticker = ticker = asyncio.create_task(self._countdown(deadline))
        try:
            while True:
                left = deadline - loop.time()
                if left <= 0:
                    return "reject"
                try:
                    line = await asyncio.wait_for(self._queue.get(), left)
                except asyncio.TimeoutError:
                    return "reject"
                if line is None:
                    return "reject"
                choice = _KEYS.get(line.strip().lower())
                if choice:
                    return choice
        finally:
            ticker.cancel()
            try:
                await ticker
            except asyncio.CancelledError:
                pass
            self._say("\n")


_DIALOG_SCRIPT = ('on run argv\n'
                  'set r to display dialog (item 2 of argv) with title (item 1 of argv) '
                  'buttons (items 4 thru -1 of argv) default button (item -1 of argv) '
                  'with icon caution giving up after ((item 3 of argv) as integer)\n'
                  'if gave up of r then return ""\n'
                  'return button returned of r\n'
                  'end run')


async def _osascript_dialog(title: str, text: str, buttons: list[str], timeout_s: float) -> str | None:
    """Show a macOS dialog over whatever is in front; the clicked button, or None on timeout / close / error."""
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", _DIALOG_SCRIPT, title, text, str(int(timeout_s)), *buttons,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    return out.decode().strip() or None


_TAGS = re.compile(r"</?session>")
_REMEMBER_S = 600
_EXCERPT_MAX = 200   # walker cuts excerpts here; a cut excerpt does not show the whole message


def _norm(excerpt: str) -> str:
    return " ".join(_TAGS.sub(" ", excerpt).lower().split())


class DialogPrompter:
    """Asks through a native macOS dialog, so a headless `hermie serve` can still ask while Claude Code is in front.
    One dialog per message: Claude Code also sends the same text again inside other requests (a session title), and
    those get the answer already given instead of a second dialog."""
    def __init__(self, timeout_s: float, runner=None, clock=None):
        self.timeout_s = timeout_s
        self.available = True
        self._run = runner or _osascript_dialog
        self._clock = clock or time.monotonic
        self._lock = asyncio.Lock()
        self._answers: list[tuple[str, str, bool, Choice, float]] = []   # (hash, normalised excerpt, whole, answer, when)

    def _remembered(self, item: PendingItem, norm: str) -> Choice | None:
        """The same item, or a whole (uncut) message that the other one contains entirely. A shared opening is not
        enough: two messages can start alike and differ in the private part."""
        now = self._clock()
        self._answers = [a for a in self._answers if now - a[4] < _REMEMBER_S]
        whole = len(item.excerpt) < _EXCERPT_MAX
        for h, text, text_whole, choice, _ in self._answers:
            if h == item.hash:
                return choice
            (short, short_whole), (long_, _) = sorted(((text, text_whole), (norm, whole)), key=lambda t: len(t[0]))
            if short_whole and len(short) >= 10 and short in long_:
                return choice
        return None

    async def ask(self, item: PendingItem) -> Choice:
        excerpt = " ".join(item.excerpt[:200].split())
        norm = _norm(item.excerpt)
        text = (f"Hermie holds this {item.kind} ({item.size} chars, reason: {item.reason}):\n\n{excerpt}\n\n"
                f"Send it to the cloud model?  (later: hermie allow {item.id or item.hash[:4]})")
        async with self._lock:
            known = self._remembered(item, norm)
            if known is not None:
                return known
            try:
                button = await self._run("Hermie: sensitive content", text, ["Reject", "Allow session", "Send"],
                                         self.timeout_s)
            except Exception:   # no osascript, no GUI session: never send
                return "reject"
            choice: Choice = {"Send": "send", "Allow session": "allow_all"}.get(button, "reject")
            self._answers.append((item.hash, norm, len(item.excerpt) < _EXCERPT_MAX, choice, self._clock()))
            return choice


_UI_FRESH_S = 90
_ANSWERS = {"send": "send", "reject": "reject", "session": "allow_all", "local": "local", "again": "again"}
_REVIEW = {"send": "send", "again": "again", "reject": "discard"}   # answers to a shown rewrite
Review = Literal["send", "again", "discard"]


def touch_ui(data_dir: Path) -> None:
    """Called by `hermie asks`: a menu in the client is polling, so a held item can wait for it."""
    p = Path(data_dir) / "ui.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(time.time()))


def read_asks(data_dir: Path) -> list[dict]:
    try:
        rows = json.loads((Path(data_dir) / "asks.json").read_text())
    except (OSError, ValueError):
        return []
    return rows if isinstance(rows, list) else []


def write_answer(data_dir: Path, ask_id: str, answer: str) -> bool:
    rows = [a for a in read_asks(data_dir) if a.get("id") == ask_id]
    if answer not in _ANSWERS or not rows:
        return False
    row = rows[0]
    if answer == "local" and row.get("local") is not True:
        return False
    if answer == "again" and row.get("kind") != "rewrite":
        return False   # "again" only answers a shown rewrite
    if row.get("kind") == "rewrite" and answer not in _REVIEW:
        return False
    _append(Path(data_dir) / "answers.jsonl", {"id": ask_id, "answer": answer, "at": time.time()})
    return True


class ModPrompter:
    """Asks through a menu in the client (the Claude Code mod polls `hermie asks --json`, answers with
    `hermie answer`). The ask file holds id, kind, reason and size only, never the held text. With no menu polling
    it hands the item to `fallback` (the macOS dialog), or rejects."""
    def __init__(self, data_dir: Path, timeout_s: float, fallback=None, poll_s: float = 0.25):
        self.data_dir = Path(data_dir)
        self.timeout_s = timeout_s
        self.fallback = fallback
        self.poll_s = poll_s
        self.available = True
        self._lock = asyncio.Lock()

    def _ui_fresh(self) -> bool:
        try:
            return time.time() - (self.data_dir / "ui.json").stat().st_mtime < _UI_FRESH_S
        except OSError:
            return False

    def _publish(self, rows: list[dict]) -> None:
        tmp = self.data_dir / "asks.json.tmp"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(rows))
        os.replace(tmp, self.data_dir / "asks.json")

    async def _wait(self, ask_id: str, row: dict) -> str | None:
        """Publish one ask row and wait for its answer; None on a timeout. The row is cleared afterwards."""
        started = time.time()
        self._publish([row])
        deadline = time.monotonic() + self.timeout_s
        try:
            while time.monotonic() < deadline:
                for r in reversed(_read_rows(self.data_dir / "answers.jsonl")):
                    if r.get("id") == ask_id and float(r.get("at", 0)) >= started:
                        return str(r.get("answer"))
                await asyncio.sleep(self.poll_s)
            return None
        finally:
            self._publish([])

    async def ask(self, item: PendingItem) -> Choice:
        if not self._ui_fresh():
            return await self.fallback.ask(item) if self.fallback is not None else "reject"
        ask_id = item.id or item.hash[:4]
        async with self._lock:
            row = {"id": ask_id, "kind": item.kind, "reason": item.reason, "size": item.size}
            if item.local_ok:
                row["local"] = True
            answer = await self._wait(ask_id, row)
            return _ANSWERS.get(answer, "reject") if answer is not None else "reject"

    async def review(self, item: PendingItem, text: str) -> Review:
        """Show the user a local rewrite (the one place the ask file holds text, and only the rewrite, only until it is
        answered): send it, rewrite again, or discard. No menu, a timeout or any other answer is a discard."""
        if not self._ui_fresh():
            return "discard"
        ask_id = item.id or item.hash[:4]
        async with self._lock:
            row = {"id": ask_id, "kind": "rewrite", "reason": item.reason, "size": len(text), "preview": text}
            return _REVIEW.get(await self._wait(ask_id, row) or "", "discard")
