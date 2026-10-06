"""Walk a JSON request body: classify every string leaf by author, scan it with the gate, rewrite the copy."""
from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass, field
from typing import Literal

from hermie.gate.types import Origin

SKIP_KEYS = {
    "model", "role", "type", "id", "name", "tool_use_id", "call_id", "stop_reason", "mime_type", "media_type",
    "enum", "required", "$schema", "format", "cache_control", "service_tier", "finish_reason", "object",
    "tool_choice", "response_format", "thought_signature",
}

WITHHELD_NOTE = ('[hermie withheld this tool result (id {id}): {size}, {reason}. '
                 'The user can release it with "hermie allow {id}".]')
WITHHELD_IMAGE = "[hermie withheld an image (id {id}); release with \"hermie allow {id}\"]"
WITHHELD_UNSCANNED = ("[hermie withheld this tool result (id {id}): {size}, {reason}. It could not be scanned, so it "
                      "cannot be released.]")
MIN_NUMBER_DIGITS = 6   # inside tool payloads, numbers with this many digits are scanned (card numbers, phone ids)

_MESSAGE_ROOTS = ("messages", "contents", "input")
_SYSTEM_ROOTS = ("system", "systemInstruction", "instructions")
_IMAGE_TYPES = ("image", "input_image", "image_url")
_TOOL_TYPES = ("tool_result", "function_call_output")
_PATH_LINE = re.compile(r"^\s*[\w./-]*\.\w+\s*$")


@dataclass
class Decision:
    kind: Literal["placeholders", "withhold", "hold", "reject", "pass"]
    id: str | None
    reason: str
    size: int
    hash: str = ""      # full hash of the leaf (or of the image data), for the allow store
    excerpt: str = ""   # hold only: the pattern-redacted text, single line, first 200 chars
    origin: str = ""    # Origin value of the leaf ("user", "tool", ...; "binary" for images)
    entities: list[str] = field(default_factory=list)   # entity names found in the leaf, sorted, unique
    new: bool = True    # False when the gate answered from its cache (the leaf was seen before)
    tool: str | None = None   # name of the tool call the leaf belongs to, when cheap to find


@dataclass
class WalkResult:
    body: dict
    decisions: list[Decision] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    scanned_bytes: int = 0
    held: Decision | None = None


def _is_image(node: dict) -> bool:
    return node.get("type") in _IMAGE_TYPES or "inlineData" in node


def classify(path: tuple[str | int, ...], body: dict) -> Origin:
    """Who wrote the string at this key path."""
    if not path:
        return Origin.OTHER
    top = path[0]
    if top in _SYSTEM_ROOTS:
        return Origin.USER
    if top not in _MESSAGE_ROOTS:
        return Origin.OTHER
    if top == "input" and len(path) == 1:
        return Origin.USER
    ancestors: list[dict] = []
    node = body
    for p in path[:-1]:
        try:
            node = node[p]
        except (KeyError, IndexError, TypeError):
            return Origin.OTHER
        if isinstance(node, dict):
            ancestors.append(node)
    if any(_is_image(a) for a in ancestors):
        return Origin.BINARY
    keys = {p for p in path if isinstance(p, str)}
    if (keys & {"functionResponse", "tool_result"}
            or any(a.get("type") in _TOOL_TYPES or a.get("role") == "tool" for a in ancestors)):
        return Origin.TOOL
    if any(a.get("role") in ("assistant", "model") or a.get("type") == "function_call" for a in ancestors):
        return Origin.ASSISTANT
    if any(a.get("role") in ("user", "system", "developer") for a in ancestors):
        return Origin.USER
    return Origin.OTHER


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _image_data(node: dict) -> str:
    src = node.get("source")
    if isinstance(src, dict) and isinstance(src.get("data"), str):
        return src["data"]
    inline = node.get("inlineData")
    if isinstance(inline, dict) and isinstance(inline.get("data"), str):
        return inline["data"]
    url = node.get("image_url")
    if isinstance(url, dict):
        url = url.get("url")
    if isinstance(url, str):
        return url
    parts: list[str] = []

    def collect(n):
        if isinstance(n, dict):
            for k, v in n.items():
                if k not in SKIP_KEYS:
                    collect(v)
        elif isinstance(n, list):
            for v in n:
                collect(v)
        elif isinstance(n, str):
            parts.append(n)

    collect(node)
    return "".join(parts)


def _image_note(node: dict, note: str) -> dict:
    if "inlineData" in node:
        return {"text": note}
    if node.get("type") == "input_image":
        return {"type": "input_text", "text": note}
    return {"type": "text", "text": note}


def _scannable_number(v) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and sum(c.isdigit() for c in repr(v)) >= MIN_NUMBER_DIGITS)


class _Walker:
    def __init__(self, gate, approvals, config, body: dict):
        self.gate, self.approvals, self.c = gate, approvals, config
        self.observe = config.mode == "observe"
        self.orig = body
        self.out = WalkResult(body=copy.deepcopy(body))
        self.images: list[tuple] = []   # handled after the text leaves, so text decisions come first
        self._names: dict[str, str] | None = None

    def _id(self, full: str) -> str:
        """Ids come from the allow store, the same source the receipt and `hermie allow` use."""
        return self.approvals.assign(full)

    def run(self) -> WalkResult:
        if self.visit(self.out.body, ()):
            for parent, key, node in self.images:
                self.image(parent, key, node)
        return self.out

    @staticmethod
    def _enters_payload(parent, key, v, path) -> bool:
        """True when v is a tool-produced or model-authored payload subtree (SKIP_KEYS do not apply inside)."""
        if not isinstance(key, str) or not isinstance(v, (dict, list)):
            return False
        parent_key = path[-2] if len(path) >= 2 else None
        kind = parent.get("type")
        return ((key == "response" and parent_key == "functionResponse")
                or (key == "args" and parent_key == "functionCall")
                or (key == "content" and kind == "tool_result")
                or (key == "input" and kind == "tool_use")
                or (key == "output" and kind == "function_call_output")
                or (key == "arguments" and kind == "function_call"))

    def visit(self, node, path, payload=False) -> bool:
        """Return False to stop the walk (a held leaf)."""
        if isinstance(node, dict):
            for k in list(node):
                if k in SKIP_KEYS and not payload:
                    continue
                if not self.child(node, k, path + (k,), payload):
                    return False
            if payload:
                return self.keys(node, path)
        elif isinstance(node, list):
            for i in range(len(node)):
                if not self.child(node, i, path + (i,), payload):
                    return False
        return True

    def child(self, parent, key, path, payload=False) -> bool:
        v = parent[key]
        payload = payload or (isinstance(parent, dict) and self._enters_payload(parent, key, v, path))
        if isinstance(v, dict) and _is_image(v):
            self.images.append((parent, key, v))
            return True
        if isinstance(v, str):
            return self.leaf(parent, key, v, path)
        if payload and _scannable_number(v):
            return self.leaf(parent, key, repr(v), path, number=True)
        return self.visit(v, path, payload)

    def keys(self, node: dict, path) -> bool:
        """Inside a tool payload the keys are data too (`{"alice@example.com": "vip"}`): scan each one and rebuild the
        dict with the redacted keys, in order; on a collision the first key keeps its value. Protocol dicts outside
        payloads keep their keys untouched."""
        renamed: dict = {}
        for k in list(node):
            if not isinstance(k, str):
                continue
            holder = {"k": k}
            if not self.leaf(holder, "k", k, path + (k,)):
                return False
            if holder["k"] != k:
                renamed[k] = holder["k"]
        if renamed and not self.observe:
            items = list(node.items())
            node.clear()
            for k, v in items:
                node.setdefault(renamed.get(k, k), v)
        return True

    def image(self, parent, key, node: dict) -> None:
        if self.c.images == "pass":
            return
        full = hashlib.sha256(_image_data(node).encode("utf-8", "surrogatepass")).hexdigest()
        did = self._id(full)
        size = len(_image_data(node).encode())
        if self.approvals.is_allowed(full, "image"):
            self.out.decisions.append(Decision("pass", did, "image released", size, full, origin="binary"))
            return
        self.out.decisions.append(Decision("withhold", did, "image", size, full, origin="binary"))
        if not self.observe:
            parent[key] = _image_note(node, WITHHELD_IMAGE.format(id=did))

    def leaf(self, parent, key, text: str, path, number: bool = False) -> bool:
        """Scan one string (or, with `number`, the text of a number: it is replaced only by a placeholder or a note)."""
        origin = classify(path, self.orig)
        if origin is Origin.BINARY:      # never scan image data
            return True
        hint = None
        if origin is Origin.TOOL:
            first = text.split("\n", 1)[0]
            if _PATH_LINE.match(first):
                hint = first.strip()
        scan_origin = Origin.TOOL if origin is Origin.OTHER else origin
        res = self.gate.scan(text, scan_origin, path_hint=hint)
        size = len(text.encode())
        self.out.scanned_bytes += size
        for f in res.findings:
            self.out.counts[f.entity] = self.out.counts.get(f.entity, 0) + 1
        flagged = res.sensitive
        # OTHER leaves are protocol content: ignore both judge and smuggling flags, but still fail closed on detector errors
        if origin is Origin.OTHER and not res.reason.startswith("detector_error"):
            flagged = False    # a judge flag on tool descriptions / metadata is ignored
        if not res.findings and not flagged:
            return True if number else self.apply(parent, key, res.text)

        meta = {"origin": origin.value, "entities": sorted({f.entity for f in res.findings}),
                "new": not res.cached, "tool": self._tool_name(path)}
        if res.findings:
            self.out.decisions.append(Decision("placeholders", None, f"{len(res.findings)} replaced", size, **meta))
        if not flagged:
            return self.apply(parent, key, res.text)
        did = self._id(res.hash)
        # a detector error has no scanned form: never released (not by `hermie allow`, not by the session switch)
        unscanned = res.reason.startswith("detector_error")
        if origin is Origin.USER:
            if not unscanned and self.approvals.is_allowed(res.hash, res.reason):
                self.out.decisions.append(Decision("pass", did, res.reason, size, res.hash, **meta))
            else:
                # a detector error's text is the raw text: no excerpt
                excerpt = "" if unscanned else " ".join(res.text[:400].split())[:200]
                d = Decision("hold", did, res.reason, size, res.hash, excerpt, **meta)
                self.out.decisions.append(d)
                if not self.observe:
                    self.out.held = d
                    return False
            return self.apply(parent, key, res.text)
        if not unscanned and self.approvals.is_allowed(res.hash, res.reason):
            self.out.decisions.append(Decision("pass", did, res.reason, size, res.hash, **meta))
            return self.apply(parent, key, res.text)
        self.out.decisions.append(Decision("withhold", did, res.reason, size, res.hash, **meta))
        note = (WITHHELD_UNSCANNED if unscanned else WITHHELD_NOTE).format(id=did, size=_fmt_size(size),
                                                                           reason=res.reason)
        return self.apply(parent, key, note)

    def _tool_names(self) -> dict[str, str]:
        """Call id -> tool name, built once per walk from tool_use / function_call / chat tool_calls entries."""
        if self._names is None:
            names: dict[str, str] = {}

            def collect(n):
                if isinstance(n, dict):
                    name = n.get("name")
                    if isinstance(name, str):
                        for k in ("id", "call_id"):
                            if isinstance(n.get(k), str):
                                names[n[k]] = name
                    fn = n.get("function")
                    if isinstance(fn, dict) and isinstance(fn.get("name"), str) and isinstance(n.get("id"), str):
                        names[n["id"]] = fn["name"]
                    for v in n.values():
                        collect(v)
                elif isinstance(n, list):
                    for v in n:
                        collect(v)

            for root in _MESSAGE_ROOTS:
                collect(self.orig.get(root))
            self._names = names
        return self._names

    def _tool_name(self, path) -> str | None:
        node = self.orig
        found = None
        for p in path[:-1]:
            try:
                node = node[p]
            except (KeyError, IndexError, TypeError):
                break
            if not isinstance(node, dict):
                continue
            kind = node.get("type")
            if kind in ("tool_use", "function_call") or "name" in node and p in ("functionCall", "functionResponse"):
                found = node.get("name")
            elif kind == "tool_result":
                found = self._tool_names().get(node.get("tool_use_id"))
            elif kind == "function_call_output":
                found = self._tool_names().get(node.get("call_id"))
            elif node.get("role") == "tool":
                found = self._tool_names().get(node.get("tool_call_id"))
        return found[:64] if isinstance(found, str) else None

    def apply(self, parent, key, new: str) -> bool:
        if not self.observe:
            parent[key] = new
        return True


def walk_request(body: dict, gate, approvals, config) -> WalkResult:
    """`approvals` provides `is_allowed(hash, reason)` and `assign(hash) -> id` (the allow store's id source)."""
    return _Walker(gate, approvals, config, body).run()
