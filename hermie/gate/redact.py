from __future__ import annotations

import fcntl
import json
import os
import re
from pathlib import Path
from typing import Callable, Optional

from .types import Finding

PLACEHOLDER = re.compile(r"<([A-Z_]+)_(\d+)>")
MAX_PLACEHOLDER_LEN = 40


def _merge_overlaps(findings: list[Finding]) -> list[Finding]:
    merged: list[Finding] = []
    for f in sorted(findings, key=lambda f: (f.start, -f.end)):
        if merged and f.start < merged[-1].end:
            if f.end > merged[-1].end:
                merged[-1].end = f.end
            continue
        merged.append(Finding(f.entity, f.start, f.end, f.score))
    return merged


def redact(text: str, findings: list[Finding],
           existing: Optional[dict[str, str]] = None) -> tuple[str, dict[str, str]]:
    """Replace sensitive spans with placeholders; returns (redacted text, new placeholder -> original).
    With `existing` (a mapping already in use for this task), an original that already has a placeholder reuses it
    and new placeholders continue the numbering, so a second redaction never mints a colliding name.
    The mapping never leaves this machine."""
    mapping: dict[str, str] = {}
    counters: dict[str, int] = {}
    seen: dict[str, str] = {}
    for ph, original in (existing or {}).items():
        seen.setdefault(original, ph)
        m = PLACEHOLDER.fullmatch(ph)
        if m:
            counters[m.group(1)] = max(counters.get(m.group(1), 0), int(m.group(2)))
    out, last = [], 0
    for f in sorted(_merge_overlaps(findings), key=lambda f: f.start):
        original = text[f.start:f.end]
        if original in seen:
            ph = seen[original]
        else:
            counters[f.entity] = counters.get(f.entity, 0) + 1
            ph = f"<{f.entity}_{counters[f.entity]}>"
            seen[original] = ph
            mapping[ph] = original
        out.append(text[last:f.start])
        out.append(ph)
        last = f.end
    out.append(text[last:])
    return "".join(out), mapping


def restore(text: str, mapping: dict[str, str]) -> tuple[str, int]:
    """One regex pass. Returns the restored text and how many placeholder-shaped tokens were not in the mapping."""
    missing = 0

    def sub(m: re.Match) -> str:
        nonlocal missing
        v = mapping.get(m.group(0))
        if v is None:
            missing += 1
            return m.group(0)
        return v

    return PLACEHOLDER.sub(sub, text), missing


class MappingStoreError(OSError):
    pass


class MappingStore:
    """placeholder -> original, persisted at `path` (mode 0600), shared between processes under flock."""

    def __init__(self, path: Path):
        self.path = path

    def _read(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text("utf-8"))
        if not isinstance(data, dict):
            raise MappingStoreError(f"{self.path.name} does not hold a mapping")
        return data

    @property
    def mapping(self) -> dict[str, str]:
        try:
            return self._read()
        except (OSError, ValueError) as e:
            raise MappingStoreError(str(e)) from e

    def _write(self, d: dict[str, str], merge: bool = False) -> None:
        """Under the lock: optionally merge `d` into the file's content, then replace the file atomically."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path.with_suffix(".lock"), "w") as lf:
                fcntl.flock(lf, fcntl.LOCK_EX)
                data = (self._read() | d) if merge else d
                tmp = self.path.with_suffix(".tmp")
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(json.dumps(data, ensure_ascii=False))
                os.chmod(tmp, 0o600)
                os.replace(tmp, self.path)
                os.chmod(self.path, 0o600)
        except (OSError, ValueError) as e:
            raise MappingStoreError(str(e)) from e

    def update(self, fn: Callable[[dict[str, str]], dict[str, str]]) -> dict[str, str]:
        """Atomically: under the flock read the file's mapping, let `fn(current)` return the NEW entries, refuse a key
        that already maps to a different value, write the merge and return it."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path.with_suffix(".lock"), "w") as lf:
                fcntl.flock(lf, fcntl.LOCK_EX)
                current = self._read()
                new = fn(dict(current))
                for k, v in new.items():
                    if k in current and current[k] != v:
                        raise MappingStoreError(f"placeholder {k} already maps to another value")
                merged = current | new
                if new:
                    tmp = self.path.with_suffix(".tmp")
                    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        f.write(json.dumps(merged, ensure_ascii=False))
                    os.chmod(tmp, 0o600)
                    os.replace(tmp, self.path)
                    os.chmod(self.path, 0o600)
                return merged
        except MappingStoreError:
            raise
        except (OSError, ValueError) as e:
            raise MappingStoreError(str(e)) from e

    def add(self, new: dict[str, str]) -> None:
        if new:
            self.update(lambda cur: new)

    def clear(self) -> None:
        self._write({})
