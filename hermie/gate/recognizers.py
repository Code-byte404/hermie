"""Rules layer of the privacy gate: Presidio recognizers (English engine, optional Chinese engine), secret
patterns, plausibility filters and the encoded-data check for outbound URLs."""
from __future__ import annotations

import math
import re
from typing import Optional
from urllib.parse import urlparse

from . import rules
from .redact import _merge_overlaps
from .rules import SECRET_PATTERNS, VALUE_ONLY
from .types import Finding

SENSITIVE_ENTITIES = {
    "PHONE_NUMBER", "US_SSN", "CREDIT_CARD", "EMAIL_ADDRESS", "IBAN_CODE", "IP_ADDRESS", "US_PASSPORT",
    "US_DRIVER_LICENSE", "PERSON", "SECRET", "CUSTOM_KEYWORD", "CN_MOBILE", "CN_ID_CARD", "BANK_CARD",
    "ADDRESS", "NATIONAL_ID",
}

_SPACY_MODELS = {"en": "en_core_web_lg", "zh": "zh_core_web_sm"}


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


# Keys / credentials: the patterns live in rules.py (shared with the regex-only detectors); Presidio runs them too.


def build_analyzer(languages: tuple[str, ...] = ("en",), deny_words: tuple[str, ...] = ()):
    from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer, RecognizerRegistry
    from presidio_analyzer.nlp_engine import NlpEngineProvider
    from presidio_analyzer.predefined_recognizers import (
        CreditCardRecognizer, EmailRecognizer, IbanRecognizer, IpRecognizer, PhoneRecognizer, SpacyRecognizer,
        UsLicenseRecognizer, UsPassportRecognizer, UsSsnRecognizer)

    class CnIdRecognizer(PatternRecognizer):
        def validate_result(self, pattern_text: str):
            return _cn_id_checksum_ok(pattern_text)

    class BankCardRecognizer(PatternRecognizer):
        def validate_result(self, pattern_text: str):
            return _luhn_ok(re.sub(r"\D", "", pattern_text))

    class ValueOnlyRecognizer(PatternRecognizer):
        """Shrinks each result to the pattern's group 1 (the value), so the key name stays readable."""

        def analyze(self, text, entities, nlp_artifacts=None, regex_flags=None):
            out = []
            for r in super().analyze(text, entities, nlp_artifacts, regex_flags):
                name = getattr(r.analysis_explanation, "pattern_name", None)
                for p in self.patterns:
                    if name not in (None, p.name) or p.name not in VALUE_ONLY:
                        continue
                    m = re.match(p.regex, text[r.start:r.end], re.IGNORECASE | re.DOTALL | re.MULTILINE)
                    if m and m.re.groups >= 1 and m.group(1):
                        r.start, r.end = r.start + m.start(1), r.start + m.end(1)
                        break
                out.append(r)
            return out

    def secret(lang: str):
        plain = [Pattern(n, rx, s) for n, rx, s in SECRET_PATTERNS if n not in VALUE_ONLY]
        valued = [Pattern(n, rx, s) for n, rx, s in SECRET_PATTERNS if n in VALUE_ONLY]
        return [PatternRecognizer(supported_entity="SECRET", supported_language=lang, patterns=plain),
                ValueOnlyRecognizer(supported_entity="SECRET", supported_language=lang, patterns=valued,
                                    name="ValueOnlySecretRecognizer")]

    recognizers = []
    for lang in languages:
        recognizers += secret(lang)
        if deny_words:
            recognizers.append(PatternRecognizer(supported_entity="CUSTOM_KEYWORD", supported_language=lang,
                                                 deny_list=list(deny_words)))
        recognizers.append(SpacyRecognizer(supported_language=lang, supported_entities=["PERSON"]))
    if "en" in languages:
        en = {"supported_language": "en"}
        recognizers += [PhoneRecognizer(**en), UsSsnRecognizer(**en), CreditCardRecognizer(**en),
                        EmailRecognizer(**en), IbanRecognizer(**en), IpRecognizer(**en),
                        UsPassportRecognizer(**en), UsLicenseRecognizer(**en),
                        # Presidio's phone / SSN validators reject reserved ranges (area code 555, 123-45-6789),
                        # which are exactly what people paste as examples; match the shape too.
                        PatternRecognizer(
                            supported_entity="PHONE_NUMBER", supported_language="en",
                            patterns=[Pattern("us_phone_shape",
                                              r"(?<![\w+-])(?:\+1[ .-]?(?:\(\d{3}\)\s?|\d{3}[ .-])\d{3}[ .-]\d{4}"
                                              r"|\(\d{3}\)\s?\d{3}[ .-]\d{4}"
                                              r"|\d{3}[-.]\d{3}[-.]\d{4})(?![\w-])", 0.6)]),
                        PatternRecognizer(
                            supported_entity="US_SSN", supported_language="en",
                            patterns=[Pattern("us_ssn_shape", r"(?<![\d-])(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}"
                                                              r"(?![\d-])", 0.6)])]
    if "zh" in languages:
        zh = "zh"
        # CJK characters count as \w, so \b is useless; use digit lookarounds instead
        recognizers += [
            CnIdRecognizer(
                supported_entity="CN_ID_CARD", supported_language=zh,
                patterns=[Pattern("cn_id", r"(?<![0-9])[1-9]\d{5}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])"
                                           r"(?:0[1-9]|[12]\d|3[01])\d{3}[0-9Xx](?![0-9])", 0.6)]),
            PatternRecognizer(
                supported_entity="CN_MOBILE", supported_language=zh,
                patterns=[Pattern("cn_mobile", r"(?<![0-9])(?:\+?86[- ]?)?1[3-9]\d{9}(?![0-9])", 0.7)]),
            BankCardRecognizer(
                supported_entity="BANK_CARD", supported_language=zh,
                patterns=[Pattern("bank_card", r"(?<![0-9])\d{16,19}(?![0-9])", 0.5)]),
            PatternRecognizer(
                supported_entity="EMAIL_ADDRESS", supported_language=zh,
                patterns=[Pattern("email", r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", 0.8)]),
            PatternRecognizer(
                supported_entity="IP_ADDRESS", supported_language=zh,
                patterns=[Pattern("ipv4", r"(?<![0-9.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}"
                                          r"(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![0-9.])", 0.6)]),
        ]

    registry = RecognizerRegistry(supported_languages=list(languages))
    for r in recognizers:
        registry.add_recognizer(r)

    nlp_engine = NlpEngineProvider(nlp_configuration={
        "nlp_engine_name": "spacy",
        "models": [{"lang_code": lang, "model_name": _SPACY_MODELS[lang]} for lang in languages],
        "ner_model_configuration": {
            "model_to_presidio_entity_mapping": {"PERSON": "PERSON", "PER": "PERSON", "ORG": "ORGANIZATION",
                                                 "GPE": "LOCATION", "LOC": "LOCATION", "FAC": "LOCATION"},
            "labels_to_ignore": ["CARDINAL", "LANGUAGE", "DATE", "TIME", "MONEY", "PERCENT", "QUANTITY",
                                 "ORDINAL", "NORP", "EVENT", "WORK_OF_ART", "LAW", "PRODUCT"],
        },
    }).create_engine()
    return AnalyzerEngine(registry=registry, nlp_engine=nlp_engine, supported_languages=list(languages))


# ---------------------------------------------------------------- scanning

_CJK = re.compile(r"[\u4e00-\u9fff]")
# Credential assignments also fire on code (`token: Optional[str] = None`, `password = request.form[...]`,
# `api_key = os.environ.get(...)`). A secret value is not a dotted identifier chain and not a bare code word.
_IDENT_CHAIN = re.compile(r"[A-Za-z_]+(?:\.[A-Za-z_]+)+")
_CODE_WORDS = {"optional", "none", "null", "true", "false", "string", "str", "int", "bool", "bytes", "any",
               "self", "undefined", "required", "default", "environ"}
_EN_PERSON = re.compile(r"[A-Z][a-z]+(?: [A-Z][a-z]+)+")


def plausible(entity: str, span: str, lang: str) -> bool:
    """Filter NER false positives. The Chinese model labels bare punctuation and Latin words as persons, so for
    `zh` a person needs CJK characters. The English model labels identifiers (RequestHandler, user_name) as
    persons, so for `en` a person must be two or more capitalized words. Every entity needs an alphanumeric."""
    if not any(c.isalnum() for c in span):
        return False
    if entity == "SECRET" and (_IDENT_CHAIN.fullmatch(span) or span.lower() in _CODE_WORDS):
        return False
    if entity == "PERSON":
        if lang == "zh":
            return bool(_CJK.search(span))
        return bool(_EN_PERSON.fullmatch(span))
    return True


def scan(analyzer, text: str, languages: tuple[str, ...], threshold: float) -> list[Finding]:
    found: list[Finding] = []
    for lang in languages:
        if lang == "zh" and not _CJK.search(text):
            continue   # the Chinese model has nothing to find in text without CJK characters
        for r in analyzer.analyze(text=text, language=lang, score_threshold=threshold):
            if r.entity_type in SENSITIVE_ENTITIES and plausible(r.entity_type, text[r.start:r.end], lang):
                found.append(Finding(r.entity_type, r.start, r.end, r.score))
    found += rules.find(text)   # credentials, ID numbers, contract names and addresses: no model needed
    return _merge_overlaps(found)


# ---------------------------------------------------------------- outbound smuggling detection
# A URL / search query can be a channel to send data out once a task touched sensitive material (commands are
# the other one). The gate recognizes plaintext entities; encoded data (base64, hex, long digit strings) gets
# past it, so rules cover that here. `smuggling_risk_url` is for URLs only (a long query string or a long digit run
# in a URL is suspicious); ordinary text uses `smuggling_risk_text`, which looks only for long high-entropy encoded
# tokens, because prose is long and commit SHAs and build ids are long digit / hex runs.

_TOKEN = re.compile(r"[A-Za-z0-9+/=_-]{20,}")
_LONG_TOKEN = re.compile(r"[A-Za-z0-9+/=_-]{32,}")
_HEX = re.compile(r"^[0-9a-fA-F]{32,}$")
_DIGITS = re.compile(r"(?<!\d)\d{10,}(?!\d)")
MAX_QUERY_CHARS = 512
TEXT_MIN_TOKEN = 32
TEXT_MIN_ENTROPY = 4.0


def _entropy(s: str) -> float:
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


def _looks_b64(tok: str) -> bool:
    core = tok.rstrip("=")
    return tok.endswith("=") or (any(c.isdigit() for c in core) and any(c.isupper() for c in core)
                                 and any(c.islower() for c in core))


def smuggling_risk_url(payload: str) -> Optional[str]:
    """Data that looks encoded inside a URL or search query; returns the reason, or None when there is no risk."""
    u = urlparse(payload) if "://" in payload else None
    query = u.query if u else payload
    if len(query) > MAX_QUERY_CHARS:
        return f"query string too long ({len(query)} chars)"
    scan_text = f"{u.path}?{u.query}#{u.fragment}" if u else payload
    tokens = _TOKEN.findall(scan_text)
    tokens += [part for tok in tokens if "/" in tok for part in tok.split("/") if len(part) >= 20]
    for tok in tokens:
        if _HEX.match(tok):
            return f"possible hex data: {tok[:24]}..."
        core = tok.rstrip("=")
        if len(core) >= 24 and _looks_b64(tok) and _entropy(core) >= 3.8:
            return f"possible base64-encoded data: {tok[:24]}..."
    if m := _DIGITS.search(scan_text):
        return f"contains a long digit string: {m.group()}"
    return None


def smuggling_risk_text(text: str) -> Optional[str]:
    """Encoded data inside ordinary text: base64 or hex tokens of TEXT_MIN_TOKEN+ characters with an entropy of at
    least TEXT_MIN_ENTROPY bits per character. No length rule and no digit-run rule (prose is long; commit SHAs and
    build ids are not data)."""
    tokens = _LONG_TOKEN.findall(text)
    tokens += [part for tok in tokens if "/" in tok for part in tok.split("/") if len(part) >= TEXT_MIN_TOKEN]
    for tok in tokens:
        core = tok.rstrip("=")
        if len(core) < TEXT_MIN_TOKEN or _entropy(core) < TEXT_MIN_ENTROPY:
            continue
        if _HEX.match(core):
            return f"possible hex data: {tok[:24]}..."
        if _looks_b64(tok):
            return f"possible base64-encoded data: {tok[:24]}..."
    return None
