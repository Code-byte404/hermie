"""Receipt log (data-free, one line per request) and the optional outbound body store."""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from hermie.config import Config

_ID = re.compile(r"[0-9a-f]{12}")


@dataclass
class ReceiptLine:
    """One proxied request. No free-text field on purpose: nothing here may carry request text.

    `upstream_error` is an exception class name only. `new_parts` entries are dicts with the
    keys `origin`, `tool`, `size`, `entities` and nothing else.
    """
    at: str
    id: str
    client: str
    upstream: str
    model: str | None
    stream: bool
    scanned_bytes: int
    replaced: dict[str, int]
    withheld: list[str]
    held: str | None
    approved_by: str | None
    unrestored: int
    judge_calls: int
    judge_ms: int
    status: int | None
    upstream_error: str | None
    mode: str
    new_parts: list[dict]
    restored: int = 0   # placeholders put back into the reply; lines written before the field exist read as 0


_FIELDS = {f.name for f in fields(ReceiptLine)}


def _parse(raw: str) -> ReceiptLine | None:
    try:
        data = json.loads(raw)
        return ReceiptLine(**{k: v for k, v in data.items() if k in _FIELDS})
    except (ValueError, TypeError, AttributeError):
        return None


def _stamp(when: datetime) -> str:
    if when.tzinfo is not None:
        when = when.astimezone(timezone.utc)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _sanitized(line: ReceiptLine) -> dict:
    """Serialize only what the receipt may hold, whatever the caller put in the containers."""
    data = {f.name: getattr(line, f.name) for f in fields(ReceiptLine)}
    replaced = {}
    for k, v in (data["replaced"] or {}).items():
        n = _int(v)
        if n is not None:
            replaced[str(k)] = n
    data["replaced"] = replaced
    data["withheld"] = [str(x) for x in (data["withheld"] or [])]
    parts = []
    for p in data["new_parts"] or []:
        if not isinstance(p, dict):
            continue
        tool = p.get("tool")
        parts.append({
            "origin": str(p.get("origin", "")),
            "tool": None if tool is None else str(tool),
            "size": _int(p.get("size")) or 0,
            "entities": [str(e) for e in (p.get("entities") or [])],
        })
    data["new_parts"] = parts
    data["restored"] = _int(data["restored"]) or 0
    data["unrestored"] = _int(data["unrestored"]) or 0
    return data


def _write_private(path: Path, data: bytes, flags: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | flags, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


class Receipt:
    def __init__(self, config: Config):
        self.path = config.receipt_path

    def write(self, line: ReceiptLine) -> None:
        text = json.dumps(_sanitized(line), ensure_ascii=False, separators=(",", ":")) + "\n"
        _write_private(self.path, text.encode("utf-8"), os.O_APPEND)

    def iter(self, since: datetime | None = None) -> Iterator[ReceiptLine]:
        if not self.path.is_file():
            return
        floor = _stamp(since) if since is not None else None
        with open(self.path, encoding="utf-8") as f:
            for raw in f:
                line = _parse(raw)
                if line is not None and (floor is None or line.at >= floor):
                    yield line

    def follow(self, poll_s: float = 0.5) -> Iterator[ReceiptLine]:
        """Yield existing lines, then tail the file (tail -F: reopens when it is replaced)."""
        f = None
        inode = None
        buf = ""
        try:
            while True:
                if f is None:
                    try:
                        f = open(self.path, encoding="utf-8")
                        inode = os.fstat(f.fileno()).st_ino
                        buf = ""
                    except FileNotFoundError:
                        f = None
                if f is not None:
                    chunk = f.readline()
                    if chunk:
                        buf += chunk
                        if buf.endswith("\n"):
                            line = _parse(buf)
                            buf = ""
                            if line is not None:
                                yield line
                        continue
                    try:
                        replaced = os.stat(self.path).st_ino != inode
                    except FileNotFoundError:
                        replaced = True
                    if replaced:
                        f.close()
                        f = None
                        continue
                time.sleep(poll_s)
        finally:
            if f is not None:
                f.close()


class BodyStore:
    def __init__(self, config: Config):
        self.enabled = config.bodies
        self.dir = config.outbound_dir
        self.cap = config.bodies_keep_mb * 1024 * 1024

    def put(self, request_id: str, body: bytes) -> Path | None:
        if not self.enabled or not _ID.fullmatch(request_id):
            return None
        path = self.dir / f"{request_id}.json"
        _write_private(path, body, os.O_TRUNC)
        self._prune(keep=path)
        return path

    def get(self, request_id: str) -> bytes | None:
        if not _ID.fullmatch(request_id):
            return None
        try:
            return (self.dir / f"{request_id}.json").read_bytes()
        except OSError:
            return None

    def _prune(self, keep: Path) -> None:
        entries = []
        for p in self.dir.glob("*.json"):
            try:
                st = p.stat()
            except OSError:
                continue
            entries.append((st.st_mtime_ns, p.name, st.st_size, p))
        entries.sort()  # oldest first; same-instant writes are ordered by id
        total = sum(e[2] for e in entries)
        for _, _, size, p in entries:
            if total <= self.cap:
                break
            if p == keep:
                # A receipt without its body is worse than a cap overrun: a body that alone
                # exceeds the cap stays.
                continue
            try:
                p.unlink()
            except OSError:
                continue
            total -= size


def client_label(user_agent: str | None) -> str:
    ua = (user_agent or "").strip()
    if not ua:
        return "unknown"
    low = ua.lower()
    if "claude-cli" in low or "claude-code" in low:
        return "claude-code"
    if "codex" in low:
        return "codex"
    if "geminicli" in low or "gemini" in low:
        return "gemini-cli"
    if "aider" in low or "litellm" in low:
        return "aider"
    return ua.split()[0]
