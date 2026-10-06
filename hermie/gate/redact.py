from __future__ import annotations

import fcntl
import json
import os
import re
import time
from dataclasses import dataclass
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


def redact(text: str, findings: list[Finding], existing: Optional[dict[str, str]] = None,
           counters: Optional[dict[str, int]] = None) -> tuple[str, dict[str, str]]:
    """Replace sensitive spans with placeholders; returns (redacted text, new placeholder -> original).
    With `existing` (a mapping already in use for this task), an original that already has a placeholder reuses it
    and new placeholders continue the numbering, so a second redaction never mints a colliding name. `counters`
    (the store's high-water mark per entity) keeps numbers of forgotten values from being reused.
    The mapping never leaves this machine."""
    mapping: dict[str, str] = {}
    counters = _max_counters(dict(counters or {}), counters_of(existing or {}))
    seen: dict[str, str] = {}
    for ph, original in (existing or {}).items():
        seen.setdefault(original, ph)
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


def counters_of(mapping: dict[str, str]) -> dict[str, int]:
    """Highest number in use per entity, from the placeholder names."""
    out: dict[str, int] = {}
    for ph in mapping:
        m = PLACEHOLDER.fullmatch(ph)
        if m:
            out[m.group(1)] = max(out.get(m.group(1), 0), int(m.group(2)))
    return out


def _max_counters(a: dict[str, int], b: dict[str, int]) -> dict[str, int]:
    return {k: max(a.get(k, 0), b.get(k, 0)) for k in a.keys() | b.keys()}


class MappingStoreError(OSError):
    pass


@dataclass(frozen=True)
class StoreState:
    mapping: dict[str, str]
    counters: dict[str, int]   # high-water mark per entity; survives clear() so numbers are never reused
    generation: int            # bumped by clear(); a running gate drops its cache when it changes


_EMPTY = StoreState({}, {}, 0)


class MappingStore:
    """placeholder -> original, persisted at `path` (mode 0600), shared between processes under flock.
    File format: {"mapping": {...}, "counters": {...}, "generation": n}; the old flat {placeholder: value} file is
    read as generation 0 and rewritten in the new format on the next write."""

    def __init__(self, path: Path):
        self.path = path
        self._snap: tuple[tuple, StoreState] | None = None

    def _read(self) -> StoreState:
        if not self.path.exists():
            return _EMPTY
        data = json.loads(self.path.read_text("utf-8"))
        if not isinstance(data, dict):
            raise MappingStoreError(f"{self.path.name} does not hold a mapping")
        if isinstance(data.get("mapping"), dict):
            mapping = data["mapping"]
            counters = data.get("counters") or {}
            generation = data.get("generation", 0)
            if not isinstance(counters, dict) or not isinstance(generation, int):
                raise MappingStoreError(f"{self.path.name} does not hold a mapping")
        else:   # the old flat format
            mapping, counters, generation = data, {}, 0
        counters = _max_counters({k: int(v) for k, v in counters.items()}, counters_of(mapping))
        return StoreState(mapping, counters, generation)

    def snapshot(self) -> StoreState:
        """The file's state, re-read only when the file changed (stat), so it is cheap to call per scan."""
        try:
            try:
                st = self.path.stat()
            except FileNotFoundError:
                return _EMPTY
            key = (st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size)
            if self._snap is not None and self._snap[0] == key:
                return self._snap[1]
            state = self._read()
        except MappingStoreError:
            raise
        except (OSError, ValueError) as e:
            raise MappingStoreError(str(e)) from e
        self._snap = (key, state)
        return state

    @property
    def mapping(self) -> dict[str, str]:
        return dict(self.snapshot().mapping)

    @property
    def counters(self) -> dict[str, int]:
        return dict(self.snapshot().counters)

    @property
    def generation(self) -> int:
        return self.snapshot().generation

    def _replace(self, state: StoreState) -> None:
        """Under the lock: replace the file atomically."""
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps({"mapping": state.mapping, "counters": state.counters,
                                "generation": state.generation}, ensure_ascii=False))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)

    def update(self, fn: Callable[[dict[str, str], dict[str, int]], dict[str, str]]) -> dict[str, str]:
        """Atomically: under the flock read the file, let `fn(current mapping, counters)` return the NEW entries,
        refuse a key that already maps to a different value, write the merge (and the raised counters) and return
        the merged mapping."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path.with_suffix(".lock"), "w") as lf:
                fcntl.flock(lf, fcntl.LOCK_EX)
                state = self._read()
                current = state.mapping
                new = fn(dict(current), dict(state.counters))
                for k, v in new.items():
                    if k in current and current[k] != v:
                        raise MappingStoreError(f"placeholder {k} already maps to another value")
                merged = current | new
                if new:
                    self._replace(StoreState(merged, _max_counters(state.counters, counters_of(new)),
                                             state.generation))
                return merged
        except MappingStoreError:
            raise
        except (OSError, ValueError) as e:
            raise MappingStoreError(str(e)) from e

    def add(self, new: dict[str, str]) -> None:
        if new:
            self.update(lambda cur, counters: new)

    def clear(self) -> None:
        """Forget every value but keep the counters, so a placeholder name is never given to another value, and bump
        the generation, so a running gate drops what it cached."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path.with_suffix(".lock"), "w") as lf:
                fcntl.flock(lf, fcntl.LOCK_EX)
                try:
                    state = self._read()
                    generation = state.generation + 1
                except (OSError, ValueError):   # an unreadable file: still clear it, with a fresh generation
                    state, generation = _EMPTY, time.time_ns()
                self._replace(StoreState({}, state.counters, generation))
        except (OSError, ValueError) as e:
            raise MappingStoreError(str(e)) from e
