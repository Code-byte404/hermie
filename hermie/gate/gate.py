"""The gate: scan text (patterns, optional local judge), cache by hash, certify outgoing bodies."""
from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from fnmatch import fnmatch

from hermie.gate.judge import ContextualJudge
from hermie.gate.recognizers import build_analyzer, scan as pattern_scan, smuggling_risk
from hermie.gate.redact import MappingStore, MappingStoreError, redact, restore
from hermie.gate.types import _GATE_TOKEN, CleanBody, Origin, ScanResult


def certify_body(data: bytes) -> CleanBody:
    """The only constructor of CleanBody; call it after redaction."""
    return CleanBody(data, _GATE_TOKEN)


class Gate:
    def __init__(self, config, analyzer=None, judge=None, store: MappingStore | None = None):
        self.c = config
        self._analyzer = analyzer
        self.judge = judge if judge is not None else (ContextualJudge(config) if config.judge else None)
        self.store = store or MappingStore(config.mapping_path)
        self._mapping = dict(self.store.mapping)
        self._cache: OrderedDict[str, ScanResult] = OrderedDict()
        self.cache_bytes = 0
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

    def scan(self, text: str, origin: Origin, path_hint: str | None = None) -> ScanResult:
        h = hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()
        # The result depends on more than the text: whether the judge applies and whether the path is allowed.
        key = f"{h}:{int(self._judged_origin(origin))}:{int(self._path_allowed(path_hint))}"
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
                return hit
        size = len(text)
        try:
            res = self._compute(text, origin, path_hint, h, size)
        except MappingStoreError:
            raise
        except Exception as e:  # fail closed; class name only, a message could carry the text
            # fail closed, but never cache: a transient outage must not block this text afterwards
            return ScanResult(text, [], True, True, f"detector_error: {type(e).__name__}", {}, h, size)
        self._remember(key, res)
        return res

    def _judged_origin(self, origin: Origin) -> bool:
        return self.judge is not None and origin in (Origin.USER, Origin.TOOL)

    def _path_allowed(self, path_hint: str | None) -> bool:
        return bool(path_hint) and any(fnmatch(path_hint, g) for g in self.c.allow_paths)

    def _compute(self, text, origin, path_hint, h, size) -> ScanResult:
        if self._path_allowed(path_hint):
            return ScanResult(text, [], False, False, "allow_path", {}, h, size)
        findings = [f for f in pattern_scan(self.analyzer, text, self.c.languages, self.c.presidio_threshold)
                    if text[f.start:f.end] not in self.c.allow_values]
        out: dict = {}

        def mint(current: dict[str, str]) -> dict[str, str]:
            out["red"], out["new"] = redact(text, findings, existing=current)
            return out["new"]

        # mint and persist in one critical section (flock across processes, lock across threads)
        with self._mint_lock:
            merged = self.store.update(mint)   # MappingStoreError propagates on purpose
        with self._lock:
            self._mapping = merged
        red, new = out["red"], out["new"]
        judged = sensitive = False
        reason = "clean"
        if self._judged_origin(origin):
            risk = smuggling_risk(red)
            if risk is not None:
                sensitive, reason = True, f"smuggling: {risk}"
            else:
                judged = True
                sensitive = bool(self.judge.is_sensitive(red))
                reason = "judge" if sensitive else "clean"
        return ScanResult(red, findings, judged, sensitive, reason, new, h, size)

    def _remember(self, key: str, res: ScanResult) -> None:
        limit = self.c.cache_mb * 1024 * 1024
        with self._lock:
            if key in self._cache:
                return
            self._cache[key] = res
            self.cache_bytes += res.size
            while self.cache_bytes > limit and self._cache:
                _, old = self._cache.popitem(last=False)
                self.cache_bytes -= old.size
