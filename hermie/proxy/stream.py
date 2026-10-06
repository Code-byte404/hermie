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
        self.path: Optional[tuple] = None   # JSON path of the leaf the held tail came from

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

    @property
    def held(self) -> str:
        return self._held

    def flush(self) -> str:
        text, self._held = self._held, ""
        self.path = None
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
            return {walk(k) if isinstance(k, str) else k: walk(v) for k, v in o.items()}   # keys can carry data
        return o

    return walk(obj), total


def _walk_buffered(o: Any, key: Optional[str], buffer: PlaceholderBuffer, restore: Restore,
                   last: list, path: tuple = (), cross: Optional[Callable[[], None]] = None) -> Any:
    """Restores in place order; `last` collects (container, key, path) of the final buffered string leaf.
    Before a buffered leaf is fed, a tail held from a leaf at another path is flushed back into that leaf: into the
    previous leaf of this event when there is one, else through `cross` (the leaf sits in an earlier event)."""

    def feed(container, k, v: str, p: tuple) -> None:
        if buffer.held and buffer.path != p:
            if last:
                c, ck, _ = last[0]
                c[ck] += buffer.flush()
            elif cross is not None:
                cross()
        container[k] = buffer.feed(v)
        buffer.path = p if buffer.held else None
        last[:] = [(container, k, p)]

    if isinstance(o, dict):
        renamed = False
        for k, v in list(o.items()):
            if isinstance(v, str):
                if k in BUFFERED_KEYS:
                    feed(o, k, v, path + (k,))
                else:
                    o[k], n = restore(v)
                    buffer.unrestored += n
            else:
                o[k] = _walk_buffered(v, k, buffer, restore, last, path + (k,), cross)
            renamed = renamed or (isinstance(k, str) and "<" in k)
        if renamed:   # keys can carry placeholders (tool arguments whose keys were data)
            items = list(o.items())
            o.clear()
            for k, v in items:
                nk = k
                if isinstance(k, str):
                    nk, n = restore(k)
                    buffer.unrestored += n
                o.setdefault(nk, v)
        return o
    if isinstance(o, list):
        for i, v in enumerate(list(o)):
            if isinstance(v, str):
                if key in BUFFERED_KEYS:
                    feed(o, i, v, path + (i,))
                else:
                    o[i], n = restore(v)
                    buffer.unrestored += n
            else:
                o[i] = _walk_buffered(v, key, buffer, restore, last, path + (i,), cross)
        return o
    return o


def _first_leaf_path(o: Any, key: Optional[str] = None, path: tuple = ()) -> Optional[tuple]:
    """Path of the first buffered string leaf, in the same order `_walk_buffered` visits them."""
    if isinstance(o, dict):
        for k, v in o.items():
            if isinstance(v, str):
                if k in BUFFERED_KEYS:
                    return path + (k,)
            else:
                r = _first_leaf_path(v, k, path + (k,))
                if r is not None:
                    return r
    elif isinstance(o, list):
        for i, v in enumerate(o):
            if isinstance(v, str):
                if key in BUFFERED_KEYS:
                    return path + (i,)
            else:
                r = _first_leaf_path(v, key, path + (i,))
                if r is not None:
                    return r
    return None


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _process(data: str, buffer: PlaceholderBuffer, restore: Restore,
             before: Optional[Callable[[Any], None]] = None, cross: Optional[Callable[[], None]] = None):
    """Returns (output data, parsed object or None, last buffered leaf or None).
    `before(obj)` runs once the payload is parsed and before anything is fed to the buffer; `cross()` flushes a held
    tail into the earlier event it came from."""
    try:
        obj = json.loads(data)
    except ValueError:
        obj = None
    if not isinstance(obj, (dict, list)):
        if before:
            before(None)
        return data, None, None
    if before:
        before(obj)
    last: list = []
    obj = _walk_buffered(obj, None, buffer, restore, last, cross=cross)
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
        self.leaf = None            # (container, key) of the last buffered leaf
        self.leaf_path: Optional[tuple] = None

    def render(self) -> bytes:
        return (self.eol.join(self.lines) + self.eol + self.eol).encode("utf-8")

    def append_text(self, text: str) -> bool:
        if self.leaf is None or self.data_index is None:
            return False
        container, key = self.leaf
        container[key] += text
        self.lines[self.data_index] = self.prefix + _dumps(self.obj)
        return True


class _Raw(_Event):
    """A block that is not valid UTF-8: passed through untouched."""

    def __init__(self, block: bytes, eol: str):
        super().__init__([], eol)
        self.block = block

    def render(self) -> bytes:
        e = self.eol.encode()
        return self.block + e + e


def _parse_event(block: str, eol: str, buffer: PlaceholderBuffer, restore: Restore,
                 before: Optional[Callable[[Any], None]] = None, cross: Optional[Callable[[], None]] = None) -> _Event:
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
    out, obj, leaf = _process("\n".join(parts), buffer, restore, before, cross)
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
    ev.obj = obj
    if leaf is not None:
        ev.leaf, ev.leaf_path = (leaf[0], leaf[1]), leaf[2]
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
    """Events are held back while the buffer holds a tail (and always for the last one), so that the tail can be
    flushed into the leaf that carried it: when a later leaf (in this event or a later one) has a different JSON
    path, when an event has no buffered leaf, or when the stream ends. Keep-alive events (no data, or type "ping")
    do not flush."""
    stats = stats if stats is not None else StreamStats()
    buffer = PlaceholderBuffer(restore)
    pending: "list[_Event]" = []
    acc = b""

    def holder() -> Optional[_Event]:
        return next((e for e in reversed(pending) if e.leaf is not None), None)

    def flush_into_holder() -> None:
        text = buffer.flush()
        if not text:
            return
        h = holder()
        if h is not None and h.append_text(text):
            return
        syn = _Event(["data: " + _dumps({"hermie_flush": text})], "\n")
        pending.append(syn)
        stats.synthetic += 1

    def before(obj: Any) -> None:
        # an event with buffered leaves is handled leaf by leaf (`cross`); one without any flushes here
        if not buffer.held:
            return
        if isinstance(obj, dict) and obj.get("type") == "ping":
            return
        if obj is not None and _first_leaf_path(obj) is not None:
            return
        flush_into_holder()

    def take(block: bytes, eol: str) -> Optional[_Event]:
        if not block.strip():
            return None
        try:
            text = block.decode("utf-8")
        except UnicodeDecodeError:
            return _Raw(block, eol)
        stats.events += 1
        return _parse_event(text, eol, buffer, restore, before, flush_into_holder)

    def push(ev: _Event) -> "list[bytes]":
        pending.append(ev)
        h = holder() if buffer.held else None
        keep = pending.index(h) if h is not None else len(pending) - 1
        out = [e.render() for e in pending[:keep]]
        del pending[:keep]
        return out

    async for chunk in upstream:
        acc += chunk
        while True:
            found = _next_block(acc)
            if found is None:
                break
            block, eol, acc = found
            ev = take(block, eol)
            if ev is not None:
                for b in push(ev):
                    yield b
    if acc.strip():
        # an unterminated final event: restore it like the others, emitted with a terminator
        ev = take(acc.rstrip(b"\r\n"), "\r\n" if b"\r\n" in acc else "\n")
        if ev is not None:
            for b in push(ev):
                yield b
    flush_into_holder()
    for e in pending:
        yield e.render()
    stats.unrestored = buffer.unrestored
