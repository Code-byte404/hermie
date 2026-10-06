"""Approvals for held / withheld content: allow store, session switch, terminal prompt."""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import re
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

KEEP = 500
_FULL_HASH = re.compile(r"[0-9a-f]{64}")
Choice = Literal["send", "reject", "allow_all"]
_KEYS: dict[str, Choice] = {"s": "send", "r": "reject", "a": "allow_all"}


@dataclass
class PendingItem:
    hash: str
    kind: str
    reason: str
    size: int
    excerpt: str
    id: str | None = None


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
        out: dict[str, PendingItem] = {}
        for r in kept:
            try:
                item = PendingItem(r["hash"], r["kind"], r["reason"], int(r["size"]), "")
            except (KeyError, TypeError, ValueError):
                continue
            want = r.get("id")
            self._place(out, item, want if isinstance(want, str) else None)
            self._persisted.add(item.hash)
        return out

    @staticmethod
    def _place(table: dict[str, PendingItem], item: PendingItem, want: str | None = None) -> str:
        for k, v in table.items():
            if v.hash == item.hash:
                table[k] = item
                item.id = k
                return k
        if want and len(want) >= 4 and item.hash.startswith(want) and want not in table:
            key = want
        else:
            key = next((item.hash[:n] for n in range(4, len(item.hash) + 1, 2) if item.hash[:n] not in table),
                       item.hash)
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
