"""Privacy gate: the only channel between this machine and DeepSeek.

1. Fail closed: any exception in Presidio or the judge model is treated as "contains private data".
2. Outbound safety is guaranteed by type: CleanText can only be created by PrivacyGate.certify()
   (which re-runs the full check) or trusted_template() (hard-coded constants with no user data only).
"""
from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from .config import Settings

if TYPE_CHECKING:
    from .judge import Judge

log = logging.getLogger(__name__)

_GATE_TOKEN = object()  # held only by this module, so CleanText can only be created by the gate


class CleanText:
    """Text certified by the privacy gate and allowed to leave this machine. Cannot be constructed elsewhere."""

    __slots__ = ("text",)

    def __init__(self, text: str, _token: object = None):
        if _token is not _GATE_TOKEN:
            raise PermissionError("CleanText can only be created by PrivacyGate")
        self.text = text

    def __repr__(self) -> str:
        return f"CleanText(len={len(self.text)})"


@dataclass
class Finding:
    entity: str
    start: int
    end: int
    score: float


@dataclass
class PrivacyVerdict:
    sensitive: bool
    findings: list[Finding] = field(default_factory=list)
    contextual_prob: Optional[float] = None
    reason: str = ""

    @property
    def contextual(self) -> bool:
        """Sensitive for reasons beyond rule entities (contextual sensitivity or a detection error);
        placeholder substitution may not be enough."""
        return self.sensitive and not self.reason.startswith("entities")


# ---------------------------------------------------------------- Presidio setup

def _cn_id_checksum_ok(s: str) -> bool:
    weights = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
    codes = "10X98765432"
    s = s.upper()
    if len(s) != 18 or not s[:17].isdigit():
        return False
    return codes[sum(int(c) * w for c, w in zip(s[:17], weights)) % 11] == s[17]


def _luhn_ok(s: str) -> bool:
    digits = [int(c) for c in s][::-1]
    total = sum(d if i % 2 == 0 else (d * 2 - 9 if d * 2 > 9 else d * 2) for i, d in enumerate(digits))
    return total % 10 == 0


# Keys / credentials: Presidio's generic recognizers don't know these, and once they reach the cloud
# they are leaked. All deterministic regexes.
SECRET_PATTERNS = [
    ("openai_style_key", r"(?<![A-Za-z0-9])sk-(?:[A-Za-z0-9]+-)?[A-Za-z0-9_-]{16,}", 0.85),
    ("aws_access_key", r"(?<![A-Za-z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Za-z0-9])", 0.85),
    ("github_token", r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{30,}", 0.85),
    ("slack_token", r"(?<![A-Za-z0-9])xox[abprs]-[A-Za-z0-9-]{10,}", 0.85),
    ("google_api_key", r"(?<![A-Za-z0-9])AIza[0-9A-Za-z_-]{35}(?![A-Za-z0-9])", 0.85),
    ("private_key_block", r"-----BEGIN [A-Z ]*PRIVATE KEY-----", 0.95),
    ("jwt", r"(?<![A-Za-z0-9])eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}", 0.8),
    # Assignment form: api_key=..., password: ..., DEEPSEEK_API_KEY="..." (at least 8 non-blank chars on the right).
    # The last two keywords are the Chinese words for "password" and "secret key" (written as \u escapes),
    # and the separator class also accepts the full-width colon (U+FF1A).
    ("credential_assignment",
     r"(?i)(?:api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|private[_-]?key|"
     r"app[_-]?secret|password|passwd|secret|token|\u5bc6\u7801|\u5bc6\u94a5)\s*[:=\uff1a]\s*[\"']?[A-Za-z0-9_\-/+=.@#$%!]{8,}", 0.7),
]


def build_analyzer(settings: Settings):
    from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer, RecognizerRegistry
    from presidio_analyzer.nlp_engine import NlpEngineProvider
    from presidio_analyzer.predefined_recognizers import SpacyRecognizer

    class CnIdRecognizer(PatternRecognizer):
        def validate_result(self, pattern_text: str):
            return _cn_id_checksum_ok(pattern_text)

    class BankCardRecognizer(PatternRecognizer):
        def validate_result(self, pattern_text: str):
            return _luhn_ok(re.sub(r"\D", "", pattern_text))

    lang = "zh"
    # CJK characters count as \w, so \b is useless; use digit lookarounds instead
    recognizers = [
        CnIdRecognizer(
            supported_entity="CN_ID_CARD", supported_language=lang,
            patterns=[Pattern("cn_id", r"(?<![0-9])[1-9]\d{5}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])"
                                       r"(?:0[1-9]|[12]\d|3[01])\d{3}[0-9Xx](?![0-9])", 0.6)]),
        PatternRecognizer(
            supported_entity="CN_MOBILE", supported_language=lang,
            patterns=[Pattern("cn_mobile", r"(?<![0-9])(?:\+?86[- ]?)?1[3-9]\d{9}(?![0-9])", 0.7)]),
        BankCardRecognizer(
            supported_entity="BANK_CARD", supported_language=lang,
            patterns=[Pattern("bank_card", r"(?<![0-9])\d{16,19}(?![0-9])", 0.5)]),
        PatternRecognizer(
            supported_entity="EMAIL_ADDRESS", supported_language=lang,
            patterns=[Pattern("email", r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", 0.8)]),
        PatternRecognizer(
            supported_entity="IP_ADDRESS", supported_language=lang,
            patterns=[Pattern("ipv4", r"(?<![0-9.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}"
                                      r"(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![0-9.])", 0.6)]),
        PatternRecognizer(
            supported_entity="SECRET", supported_language=lang,
            patterns=[Pattern(name, regex, score) for name, regex, score in SECRET_PATTERNS]),
        SpacyRecognizer(supported_language=lang,
                        supported_entities=["PERSON", "ORGANIZATION", "LOCATION"]),
    ]
    if settings.custom_keywords:
        recognizers.append(PatternRecognizer(
            supported_entity="CUSTOM_KEYWORD", supported_language=lang,
            deny_list=list(settings.custom_keywords)))

    registry = RecognizerRegistry(supported_languages=[lang])
    for r in recognizers:
        registry.add_recognizer(r)

    nlp_engine = NlpEngineProvider(nlp_configuration={
        "nlp_engine_name": "spacy",
        "models": [{"lang_code": lang, "model_name": "zh_core_web_sm"}],
        "ner_model_configuration": {
            "model_to_presidio_entity_mapping": {"PERSON": "PERSON", "ORG": "ORGANIZATION", "GPE": "LOCATION",
                                                 "LOC": "LOCATION", "FAC": "LOCATION"},
            "labels_to_ignore": ["CARDINAL", "LANGUAGE", "DATE", "TIME", "MONEY", "PERCENT", "QUANTITY",
                                 "ORDINAL", "NORP", "EVENT", "WORK_OF_ART", "LAW", "PRODUCT"],
        },
    }).create_engine()
    return AnalyzerEngine(registry=registry, nlp_engine=nlp_engine, supported_languages=[lang])


# ---------------------------------------------------------------- the gate

CONTEXTUAL_PRIVACY_QUESTION = (
    "Does this material contain sensitive information that should not be sent to an external third party? "
    "This includes: an individual's health/financial/family/legal situation, unpublished internal company "
    "information (layoffs, mergers and acquisitions, financial results, customer lists, source code, secret keys), "
    "or any description that identifies a specific person. Answer no if it is only public common knowledge "
    "or a general question."
)

_analyzer_lock = threading.Lock()


class PrivacyGate:
    def __init__(self, settings: Settings, judge: "Optional[Judge]" = None, analyzer=None):
        self.s = settings
        self.judge = judge
        self._analyzer = analyzer  # injectable (for tests), otherwise lazily built

    @property
    def analyzer(self):
        with _analyzer_lock:
            if self._analyzer is None:
                self._analyzer = build_analyzer(self.s)
        return self._analyzer

    def warm_up(self) -> None:
        """Load spaCy ahead of time so the first request does not stall."""
        self.analyzer.analyze(text="warm up", language="zh")

    def _presidio(self, text: str) -> list[Finding]:
        results = self.analyzer.analyze(text=text, language="zh",
                                        score_threshold=self.s.presidio_score_threshold)
        return [Finding(r.entity_type, r.start, r.end, r.score) for r in results
                if r.entity_type in self.s.sensitive_entities and _plausible(r.entity_type, text[r.start:r.end])]

    def check(self, text: str, use_judge: bool = True) -> PrivacyVerdict:
        if not text.strip():
            return PrivacyVerdict(False, reason="empty")
        try:
            findings = self._presidio(text)
        except Exception as e:  # fail closed
            log.exception("Presidio check failed; treating as private")
            return PrivacyVerdict(True, reason=f"presidio_error: {e}")

        ctx_prob = None
        if use_judge and self.judge is not None:
            try:
                ctx_prob = self.judge.noul(text, CONTEXTUAL_PRIVACY_QUESTION)
            except Exception as e:
                log.exception("Judge model failed; treating as private")
                return PrivacyVerdict(True, findings, reason=f"judge_error: {e}")

        if ctx_prob is not None and ctx_prob >= self.s.contextual_privacy_threshold:
            return PrivacyVerdict(True, findings, ctx_prob, reason=f"contextual p={ctx_prob:.2f}")
        if findings:
            kinds = sorted({f.entity for f in findings})
            return PrivacyVerdict(True, findings, ctx_prob, reason=f"entities: {kinds}")
        return PrivacyVerdict(False, [], ctx_prob, reason="clean")

    def contextual(self, text: str) -> PrivacyVerdict:
        """Contextual check via the judge model only (for when the rules layer already ran separately). Fails closed."""
        if not text.strip() or self.judge is None:
            return PrivacyVerdict(False, reason="empty")
        try:
            p = self.judge.noul(text, CONTEXTUAL_PRIVACY_QUESTION)
        except Exception as e:
            log.exception("Judge model failed; treating as private")
            return PrivacyVerdict(True, reason=f"judge_error: {e}")
        if p >= self.s.contextual_privacy_threshold:
            return PrivacyVerdict(True, [], p, reason=f"contextual p={p:.2f}")
        return PrivacyVerdict(False, [], p, reason="clean")

    def certify(self, text: str) -> CleanText:
        """Last check before anything goes out. Raises PermissionError on failure; the caller must stay local."""
        verdict = self.check(text)
        if verdict.sensitive:
            raise PermissionError(f"Outbound check failed: {verdict.reason}")
        return CleanText(text, _GATE_TOKEN)

    @staticmethod
    def trusted_template(text: str) -> CleanText:
        """Only for prompt templates hard-coded in the source (containing no user data); skips the check."""
        return CleanText(text, _GATE_TOKEN)

    @staticmethod
    def redact(text: str, findings: list[Finding]) -> tuple[str, dict[str, str]]:
        """Replace sensitive spans with placeholders; returns (redacted text, placeholder -> original).
        The mapping never leaves this machine."""
        mapping: dict[str, str] = {}
        counters: dict[str, int] = {}
        seen: dict[str, str] = {}
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

    @staticmethod
    def restore(text: str, mapping: dict[str, str]) -> str:
        for ph, original in mapping.items():
            text = text.replace(ph, original)
        return text


_NER_ENTITIES = {"PERSON", "ORGANIZATION", "LOCATION"}
_CJK = re.compile(r"[\u4e00-\u9fff]")


def _plausible(entity: str, span: str) -> bool:
    """Filter the Chinese NER model's systematic false positives: it labels bare punctuation (JSON "[]") and
    Latin words (txt, How) as person names. The Chinese model is only meaningful for person/org/location
    spans that contain CJK characters; Latin-script names are covered by the judge's contextual check."""
    if not any(c.isalnum() for c in span):
        return False
    if entity in _NER_ENTITIES:
        return bool(_CJK.search(span))
    return True


def _merge_overlaps(findings: list[Finding]) -> list[Finding]:
    merged: list[Finding] = []
    for f in sorted(findings, key=lambda f: (f.start, -f.end)):
        if merged and f.start < merged[-1].end:
            if f.end > merged[-1].end:
                merged[-1].end = f.end
            continue
        merged.append(Finding(f.entity, f.start, f.end, f.score))
    return merged
