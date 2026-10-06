"""Response path: put placeholders back into JSON replies and SSE streams.

A placeholder such as `<PHONE_NUMBER_1>` may arrive split across two delta events, so text that can still become a
placeholder is held back until it is complete (or can no longer be one)."""
from __future__ import annotations

import json
import re
from typing import Any, AsyncIterator, Callable, Optional

from ..gate.redact import MAX_PLACEHOLDER_LEN

Restore = Callable[[str], "tuple[str, int]"]

# Leaf keys whose string values are streamed text and therefore pass through the buffer.
BUFFERED_KEYS = {"text", "partial_json", "content", "arguments", "delta"}

_OPEN_TAIL = re.compile(r"<[A-Z_0-9]*\Z")


class PlaceholderBuffer:
    """Holds back a tail that may be the start of a placeholder. `unrestored` sums the restore() counts."""

    def __init__(self, restore: Restore):
        self._restore = restore
        self._held = ""
        self.unrestored = 0

    def _emit(self, text: str) -> str:
        out, n = self._restore(text)
        self.unrestored += n
        return out

    def feed(self, s: str) -> str:
        text = self._held + s
        self._held = ""
        m = _OPEN_TAIL.search(text)
        if m and len(text) - m.start() < MAX_PLACEHOLDER_LEN:
            self._held = text[m.start():]
            text = text[:m.start()]
        return self._emit(text) if text else ""

    def flush(self) -> str:
        text, self._held = self._held, ""
        return self._emit(text) if text else ""


def restore_json(obj: Any, restore: Restore) -> "tuple[Any, int]":
    """Every string leaf restored (no buffering); returns the new object and the summed unrestored count."""
    total = 0

    def walk(o: Any) -> Any:
        nonlocal total
        if isinstance(o, str):
            s, n = restore(o)
            total += n
            return s
        if isinstance(o, list):
            return [walk(x) for x in o]
        if isinstance(o, dict):
            return {k: walk(v) for k, v in o.items()}
        return o

    return walk(obj), total


def _walk_buffered(o: Any, key: Optional[str], buffer: PlaceholderBuffer, restore: Restore,
                   last: list) -> Any:
    """Restores in place order; `last` collects (container, key) of the final buffered string leaf."""
    if isinstance(o, dict):
        for k, v in list(o.items()):
            if isinstance(v, str):
                if k in BUFFERED_KEYS:
                    o[k] = buffer.feed(v)
                    last[:] = [(o, k)]
                else:
                    o[k], n = restore(v)
                    buffer.unrestored += n
            else:
                o[k] = _walk_buffered(v, k, buffer, restore, last)
        return o
    if isinstance(o, list):
        for i, v in enumerate(list(o)):
            if isinstance(v, str):
                if key in BUFFERED_KEYS:
                    o[i] = buffer.feed(v)
                    last[:] = [(o, i)]
                else:
                    o[i], n = restore(v)
                    buffer.unrestored += n
            else:
                o[i] = _walk_buffered(v, key, buffer, restore, last)
        return o
    return o


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _process(data: str, buffer: PlaceholderBuffer, restore: Restore):
    """Returns (output data, parsed object or None, last buffered leaf or None)."""
    try:
        obj = json.loads(data)
    except ValueError:
        return data, None, None
    if not isinstance(obj, (dict, list)):
        return data, None, None
    last: list = []
    obj = _walk_buffered(obj, None, buffer, restore, last)
    return _dumps(obj), obj, (last[0] if last else None)


def restore_event(data: str, buffer: PlaceholderBuffer, restore: Restore) -> str:
    return _process(data, buffer, restore)[0]


class StreamStats:
    def __init__(self) -> None:
        self.unrestored: int = 0
        self.events: int = 0
        self.synthetic: int = 0


class _Event:
    def __init__(self, lines: "list[str]", eol: str):
        self.lines = lines
        self.eol = eol
        self.data_index: Optional[int] = None
        self.prefix = "data: "
        self.obj: Any = None
        self.leaf = None

    def render(self) -> bytes:
        return (self.eol.join(self.lines) + self.eol + self.eol).encode("utf-8")

    def append_text(self, text: str) -> bool:
        if self.leaf is None or self.data_index is None:
            return False
        container, key = self.leaf
        container[key] += text
        self.lines[self.data_index] = self.prefix + _dumps(self.obj)
        return True


def _parse_event(block: str, eol: str, buffer: PlaceholderBuffer, restore: Restore) -> _Event:
    lines = block.split(eol)
    ev = _Event(lines, eol)
    idxs = [i for i, ln in enumerate(lines) if ln.startswith("data:")]
    if not idxs:
        return ev
    parts = []
    for i in idxs:
        v = lines[i][5:]
        if i == idxs[0]:
            ev.prefix = "data: " if v.startswith(" ") else "data:"
        parts.append(v[1:] if v.startswith(" ") else v)
    out, obj, leaf = _process("\n".join(parts), buffer, restore)
    if obj is None and len(idxs) > 1:
        # multi-line non-JSON data: still restore text, keep the line structure
        for i in idxs:
            v = lines[i][5:]
            sp = " " if v.startswith(" ") else ""
            r, n = buffer._restore(v[1:] if sp else v)
            buffer.unrestored += n
            lines[i] = "data:" + sp + r
        return ev
    ev.data_index = idxs[0]
    ev.obj, ev.leaf = obj, leaf
    lines[idxs[0]] = ev.prefix + out
    for i in reversed(idxs[1:]):
        del lines[i]
    return ev


def _next_block(buf: bytes):
    """First complete event in buf: (block bytes, eol, rest) or None."""
    best = None
    for sep, eol in ((b"\r\n\r\n", "\r\n"), (b"\n\n", "\n")):
        i = buf.find(sep)
        if i != -1 and (best is None or i < best[0]):
            best = (i, sep, eol)
    if best is None:
        return None
    i, sep, eol = best
    return buf[:i], eol, buf[i + len(sep):]


async def relay_sse(upstream: AsyncIterator[bytes], restore: Restore,
                    stats: Optional[StreamStats] = None) -> AsyncIterator[bytes]:
    stats = stats if stats is not None else StreamStats()
    buffer = PlaceholderBuffer(restore)
    pending: Optional[_Event] = None
    acc = b""

    def take(block: bytes, eol: str) -> Optional[_Event]:
        if not block.strip():
            return None
        try:
            text = block.decode("utf-8")
        except UnicodeDecodeError:
            ev = _Event([], eol)
            ev.render = lambda b=block, e=eol.encode(): b + e + e  # type: ignore[method-assign]
            return ev
        stats.events += 1
        return _parse_event(text, eol, buffer, restore)

    async for chunk in upstream:
        acc += chunk
        while True:
            found = _next_block(acc)
            if found is None:
                break
            block, eol, acc = found
            ev = take(block, eol)
            if ev is None:
                continue
            if pending is not None:
                yield pending.render()
            pending = ev
    tail = acc
    if tail.strip():
        # an unterminated final event: restore it like the others
        ev = take(tail.rstrip(b"\r\n"), "\r\n" if b"\r\n" in tail else "\n")
        if ev is not None:
            if pending is not None:
                yield pending.render()
            pending = ev
    flushed = buffer.flush()
    if flushed:
        if pending is None or not pending.append_text(flushed):
            if pending is not None:
                yield pending.render()
            pending = _Event(["data: " + _dumps({"hermie_flush": flushed})], "\n")
            stats.synthetic += 1
    if pending is not None:
        yield pending.render()
    stats.unrestored = buffer.unrestored
