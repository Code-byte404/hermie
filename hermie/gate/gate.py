"""The gate: scan text (patterns, known values, optional local judge), cache by hash, certify outgoing bodies."""
from __future__ import annotations

import dataclasses
import hashlib
import re
import threading
from collections import OrderedDict
from fnmatch import fnmatch
from urllib.parse import urlparse

from hermie.gate.judge import make_judge
from hermie.gate.recognizers import build_analyzer, scan as pattern_scan, smuggling_risk_text, smuggling_risk_url
from hermie.gate.redact import PLACEHOLDER, MappingStore, MappingStoreError, _merge_overlaps, redact, restore
from hermie.gate.types import _GATE_TOKEN, CleanBody, Finding, Origin, ScanResult

# spaCy refuses texts over 1,000,000 characters and Presidio's cost grows with the text: scan in chunks.
CHUNK_CHARS = 48 * 1024
CHUNK_OVERLAP = 256      # a fixed-size cut (no newline in the chunk) re-scans this much of the previous chunk
MIN_KNOWN_LEN = 4        # shorter mapping values are not searched for literally (noise)
MIN_SPAN_LEN = 3         # shorter ASCII local-model spans ("ID", "HR") are dropped: they match too much


def certify_body(data: bytes) -> CleanBody:
    """The only constructor of CleanBody; call it after redaction."""
    return CleanBody(data, _GATE_TOKEN)


def empty_body() -> CleanBody:
    """The only other constructor: an empty body has nothing to certify (GET / DELETE passthrough)."""
    return CleanBody(b"", _GATE_TOKEN)


def _trie_pattern(words: list[str]) -> str:
    """A regex alternation of literal words built as a trie, so matching costs the text length, not the word count.
    At every branch the longer continuation is tried first, so the longest word at a position wins."""
    trie: dict = {}
    for w in words:
        node = trie
        for ch in w:
            node = node.setdefault(ch, {})
        node[""] = {}

    def emit(node: dict) -> str:
        alts = []
        for ch in sorted(k for k in node if k):
            run, child = [ch], node[ch]
            while len(child) == 1 and "" not in child:   # compress a chain without branches or word ends
                (c, nxt), = child.items()
                run.append(c)
                child = nxt
            alts.append(re.escape("".join(run)) + emit(child))
        if not alts:
            return ""
        body = alts[0] if len(alts) == 1 else "(?:" + "|".join(alts) + ")"
        return f"(?:{body})?" if "" in node else body

    return emit(trie)


def _compile_known(words: list[str]) -> re.Pattern | None:
    if not words:
        return None
    try:
        return re.compile(_trie_pattern(words))
    except (RecursionError, re.error, OverflowError):   # pathological sets: plain alternation, longest first
        return re.compile("|".join(re.escape(w) for w in sorted(words, key=len, reverse=True)))


def _is_url(text: str) -> bool:
    s = text.strip()
    if not s or any(c.isspace() for c in s) or "://" not in s:
        return False
    try:
        u = urlparse(s)
    except ValueError:
        return False
    return bool(u.scheme and u.netloc)


# Claude Code injects its own instructions (CLAUDE.md, memory, hook output) into user messages in these blocks. They are
# not what the person wrote, so the judge does not weigh them; the patterns and known values still scan them.
_INJECTED = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)


def strip_injected(text: str) -> str:
    return _INJECTED.sub("", text)


def _ascii_word(ch: str) -> bool:
    return ch.isascii() and (ch.isalnum() or ch == "_")


# A local-model span that is one lower-case English word (`layoff`, `data`) is sensitive in that message, not
# everywhere: its placeholder is restored in replies but the value is not searched for in later requests.
_GENERIC_WORD = re.compile(r"[a-z]+(?:-[a-z]+)*")


class Gate:
    def __init__(self, config, analyzer=None, judge=None, store: MappingStore | None = None):
        self.c = config
        self._analyzer = analyzer
        self.judge = judge if judge is not None else (make_judge(config) if config.judge else None)
        self.store = store or MappingStore(config.mapping_path)
        self._verdicts = self._load_verdicts()
        snap = self.store.snapshot()
        self._mapping = dict(snap.mapping)
        self._gen = snap.generation
        self._quiet = snap.quiet
        # cache value: (result, generation, number of mapping entries already searched for literally)
        self._cache: OrderedDict[str, tuple[ScanResult, int, int]] = OrderedDict()
        self.cache_bytes = 0
        self._known: tuple[tuple, re.Pattern | None, dict[str, str]] | None = None
        self._lock = threading.Lock()
        self._mint_lock = threading.Lock()
        self._analyzer_lock = threading.Lock()

    @property
    def analyzer(self):
        with self._analyzer_lock:
            if self._analyzer is None:
                self._analyzer = build_analyzer(self.c.languages, self.c.deny_words)
            return self._analyzer

    @property
    def mapping(self) -> dict[str, str]:
        with self._lock:
            return dict(self._mapping)

    def restore(self, text: str) -> tuple[str, int]:
        with self._lock:
            mapping = self._mapping
        return restore(text, mapping)

    def _sync(self) -> None:
        """Follow the store: after `hermie forget` (a new generation) the cache and the in-memory mapping are dropped;
        entries another process added are picked up."""
        snap = self.store.snapshot()
        with self._lock:
            if snap.generation != self._gen or not snap.quiet >= self._quiet:
                # a new generation, or a quiet value searched again (`hermie hide`): cached results did not look
                # for it
                self._cache.clear()
                self.cache_bytes = 0
                self._gen = snap.generation
                self._mapping = dict(snap.mapping)
            elif len(snap.mapping) > len(self._mapping):
                self._mapping = dict(snap.mapping)
            self._quiet = snap.quiet

    def scan(self, text: str, origin: Origin, path_hint: str | None = None) -> ScanResult:
        self._sync()   # MappingStoreError propagates on purpose
        h = hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()
        # The result depends on more than the text: whether the judge applies and whether the path is allowed.
        key = f"{h}:{int(self._judged_origin(origin))}:{int(self._path_allowed(path_hint))}"
        with self._lock:
            hit = self._cache.get(key)
            gen, mapping = self._gen, self._mapping
            if hit is not None and hit[1] == gen:
                self._cache.move_to_end(key)
        if hit is not None and hit[1] == gen:
            res, _, searched = hit
            if len(mapping) > searched and res.reason != "allow_path":
                res = self._late_known(res, mapping, searched)
                with self._lock:
                    if key in self._cache:
                        self._cache[key] = (res, gen, len(mapping))
            return dataclasses.replace(res, cached=True)   # a copy: the cached object never changes
        size = len(text)
        try:
            res, searched = self._compute(text, origin, path_hint, h, size)
        except MappingStoreError:
            raise
        except Exception as e:  # fail closed; class name only, a message could carry the text
            # fail closed, but never cache: a transient outage must not block this text afterwards
            return ScanResult(text, [], True, True, f"detector_error: {type(e).__name__}", {}, h, size)
        self._remember(key, res, gen, searched)
        return res

    def redact_spans(self, text: str, spans: list[tuple[str, str]]) -> str:
        """Replace every literal occurrence of each (value, entity) span with its placeholder, minting and persisting
        new ones like a scan does. Used for spans a local model found; the placeholders restore in the reply."""
        self._sync()   # MappingStoreError propagates on purpose
        taken = [(m.start(), m.end()) for m in PLACEHOLDER.finditer(text)]
        findings = []
        for value, entity in spans:
            if len(value.strip()) < (MIN_SPAN_LEN if value.isascii() else 2) or PLACEHOLDER.search(value):
                continue
            # whole words only (`Lin` is not in `Linda`), and never inside a placeholder already in the text
            # (ASCII edges only: Chinese has no spaces between words)
            rx = re.compile((r"(?<![A-Za-z0-9_])" if _ascii_word(value[0]) else "") + re.escape(value)
                            + (r"(?![A-Za-z0-9_])" if _ascii_word(value[-1]) else ""))
            for m in rx.finditer(text):
                if not any(s < m.end() and m.start() < e for s, e in taken):
                    findings.append(Finding(entity, m.start(), m.end(), 1.0))
        findings = _merge_overlaps(findings)
        generic = {text[f.start:f.end] for f in findings if _GENERIC_WORD.fullmatch(text[f.start:f.end])}
        out: dict = {}

        def mint(current: dict[str, str], counters: dict[str, int]) -> dict[str, str]:
            out["red"], new = redact(text, findings, existing=current, counters=counters)
            return new

        with self._mint_lock:
            merged = self.store.update(mint, quiet=generic)
            quiet = self.store.snapshot().quiet
            with self._lock:
                self._mapping = merged
                self._quiet = quiet
        return out["red"]

    _VERDICT_KEEP = 50_000

    def _verdict_key(self, text: str) -> str:
        return hashlib.sha256(f"{self.c.judge}\0{text}".encode("utf-8", "surrogatepass")).hexdigest()

    def _load_verdicts(self) -> dict[str, bool]:
        path = self.c.judge_cache_path
        out: dict[str, bool] = {}
        try:
            for line in (path.read_text("utf-8").splitlines() if path is not None and path.exists() else []):
                k, _, v = line.partition(" ")
                if len(k) == 64 and v in ("0", "1"):
                    out[k] = v == "1"
        except (OSError, ValueError):
            return {}
        return dict(list(out.items())[-self._VERDICT_KEEP:])

    def _judge(self, text: str) -> bool:
        """The judge's verdict, remembered across restarts (a restart would otherwise re-judge a whole conversation
        history). Errors propagate and are never stored."""
        key = self._verdict_key(text)
        known = self._verdicts.get(key)
        if known is not None:
            return known
        verdict = bool(self.judge.is_sensitive(text))
        self._verdicts[key] = verdict
        path = self.c.judge_cache_path
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as f:
                    f.write(f"{key} {int(verdict)}\n")
            except OSError:
                pass   # the verdict still holds for this process
        return verdict

    def _judged_origin(self, origin: Origin) -> bool:
        return self.judge is not None and origin in (Origin.USER, Origin.TOOL)

    def _path_allowed(self, path_hint: str | None) -> bool:
        return bool(path_hint) and any(fnmatch(path_hint, g) for g in self.c.allow_paths)

    def _pattern_findings(self, text: str) -> list[Finding]:
        """Presidio over the text in chunks of CHUNK_CHARS cut at newlines. A finding that runs into the end of a
        chunk (a private key block cut in half) makes the next chunk start at that finding, and a fixed-size cut
        (no newline) overlaps the next chunk by CHUNK_OVERLAP, so no entity is lost at a boundary."""
        langs, thr = self.c.languages, self.c.presidio_threshold
        n = len(text)
        if n <= CHUNK_CHARS:
            return pattern_scan(self.analyzer, text, langs, thr)
        found: list[Finding] = []
        pos = 0
        while pos < n:
            end, fixed = n, False
            if n - pos > CHUNK_CHARS:
                cut = text.rfind("\n", pos, pos + CHUNK_CHARS)
                end, fixed = (cut + 1, False) if cut > pos else (pos + CHUNK_CHARS, True)
            chunk = pattern_scan(self.analyzer, text[pos:end], langs, thr)
            found += [Finding(f.entity, f.start + pos, f.end + pos, f.score) for f in chunk]
            if end >= n:
                break
            nxt = end - CHUNK_OVERLAP if fixed else end
            # a finding followed only by whitespace up to the cut may continue in the next chunk
            touching = [pos + f.start for f in chunk if pos + f.end >= end or not text[pos + f.end:end].strip()]
            # a private key whose END line is past the cut: the next chunk starts at its BEGIN line, so the key is
            # scanned whole (its body lines alone could pass for ordinary text)
            kb = text.rfind("-----BEGIN", pos, end)
            if kb >= 0 and "PRIVATE KEY" in text[kb:kb + 64] and "-----END" not in text[kb:end]:
                touching.append(kb)
            if touching:
                nxt = min(nxt, min(touching))
            pos = nxt if nxt > pos else end
        return _merge_overlaps(found)

    def _known_regex(self, mapping: dict[str, str]) -> tuple[re.Pattern | None, dict[str, str]]:
        """Compiled search for every mapping value (value -> entity). Mappings only grow within a generation and a
        placeholder name is never reused, so (size, last name, quiet names) identifies the content."""
        ident = (len(mapping), next(reversed(mapping), None), self._quiet)
        known = self._known
        if known is not None and known[0] == ident:
            return known[1], known[2]
        rx, entity = self._build_known(list(mapping.items()))
        self._known = (ident, rx, entity)
        return rx, entity

    def _build_known(self, items) -> tuple[re.Pattern | None, dict[str, str]]:
        entity: dict[str, str] = {}
        for ph, value in items:
            m = PLACEHOLDER.fullmatch(ph)
            if (m and ph not in self._quiet and isinstance(value, str) and len(value) >= MIN_KNOWN_LEN
                    and value not in self.c.allow_values):
                entity.setdefault(value, m.group(1))
        return _compile_known(list(entity)), entity

    @staticmethod
    def _known_findings(text: str, rx: re.Pattern | None, entity: dict[str, str]) -> list[Finding]:
        """Literal occurrences of known values (their placeholder's entity), never inside a placeholder."""
        if rx is None:
            return []
        taken = [(m.start(), m.end()) for m in PLACEHOLDER.finditer(text)]
        out = []
        for m in rx.finditer(text):
            if m.end() == m.start():
                continue
            if any(s < m.end() and m.start() < e for s, e in taken):
                continue
            out.append(Finding(entity[m.group(0)], m.start(), m.end(), 1.0))
        return out

    def _late_known(self, res: ScanResult, mapping: dict[str, str], searched: int) -> ScanResult:
        """A cached result was computed before some values entered the mapping: search for those values now."""
        rx, entity = self._build_known(list(mapping.items())[searched:])
        extra = self._known_findings(res.text, rx, entity)
        if not extra:
            return res
        red, _ = redact(res.text, extra, existing=mapping)
        return dataclasses.replace(res, text=red, findings=res.findings + extra)

    def _compute(self, text, origin, path_hint, h, size) -> tuple[ScanResult, int]:
        if self._path_allowed(path_hint):
            return ScanResult(text, [], False, False, "allow_path", {}, h, size), 0
        findings = [f for f in self._pattern_findings(text) if text[f.start:f.end] not in self.c.allow_values]
        out: dict = {}

        def mint(current: dict[str, str], counters: dict[str, int]) -> dict[str, str]:
            # values already in the mapping (restored into assistant text, tool calls and results on the way back)
            # map back to their placeholders even where the recognizers would not see them
            rx, entity = self._known_regex(current)
            out["all"] = _merge_overlaps(findings + self._known_findings(text, rx, entity))
            out["red"], out["new"] = redact(text, out["all"], existing=current, counters=counters)
            return out["new"]

        # mint and persist in one critical section (flock across processes, lock across threads)
        with self._mint_lock:
            merged = self.store.update(mint)   # MappingStoreError propagates on purpose
            with self._lock:
                self._mapping = merged
        red, new, findings = out["red"], out["new"], out["all"]
        judged = sensitive = False
        reason = "clean"
        if self._judged_origin(origin):
            # the client injects <system-reminder> blocks into the user's turn; anywhere else (a file, a tool's
            # output) the tags are just text and could wrap anything, so they are judged too
            own = strip_injected(red) if origin is Origin.USER else red
            risk = smuggling_risk_url(red.strip()) if _is_url(red) else smuggling_risk_text(own)
            if risk is not None:
                sensitive, reason = True, f"smuggling: {risk}"
            else:
                judged = True
                sensitive = bool(own.strip()) and self._judge(own)
                reason = "judge" if sensitive else "clean"
        return ScanResult(red, findings, judged, sensitive, reason, new, h, size), len(merged)

    def _remember(self, key: str, res: ScanResult, gen: int, searched: int) -> None:
        limit = self.c.cache_mb * 1024 * 1024
        with self._lock:
            if key in self._cache or gen != self._gen:
                return
            self._cache[key] = (res, gen, searched)
            self.cache_bytes += res.size
            while self.cache_bytes > limit and self._cache:
                _, (old, _, _) = self._cache.popitem(last=False)
                self.cache_bytes -= old.size
