"""The eval sets double as a regression test: what the rules layer can catch must be caught, cases labeled
normal must not be flagged."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermie.gate.recognizers import scan

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from evals.run_evals import load_cases, privacy_metrics  # noqa: E402

EVALS = Path(__file__).resolve().parent.parent / "evals"


def verdicts(analyzer, cases, langs):
    out = []
    for c in cases:
        findings = scan(analyzer, c["text"], langs, 0.5)
        out.append(SimpleNamespace(sensitive=bool(findings), findings=findings,
                                   reason=f"entities: {sorted({f.entity for f in findings})}"))
    return out


def check(m, entities):
    rule_misses = [c for c in m["misses"] if c.get("layer") == "rules"]
    assert not rule_misses, [c["text"] for c in rule_misses]
    assert not m["false_alarms"], [(c["text"], r) for c, r in m["false_alarms"]]
    for e in entities:
        hit, n = m["per_entity"][e]
        assert n > 0 and hit == n, (e, hit, n)


def test_english_cases(analyzer):
    cases = load_cases(EVALS / "privacy_cases_en.jsonl")
    assert len(cases) >= 40
    m = privacy_metrics(cases, verdicts(analyzer, cases, ("en",)), with_judge=False)
    check(m, ("PHONE_NUMBER", "US_SSN", "CREDIT_CARD", "EMAIL_ADDRESS", "SECRET", "PERSON", "ADDRESS", "NATIONAL_ID"))


def test_chinese_cases(analyzer_zh):
    cases = load_cases(EVALS / "privacy_cases.jsonl")
    m = privacy_metrics(cases, verdicts(analyzer_zh, cases, ("en", "zh")), with_judge=False)
    check(m, ("CN_MOBILE", "CN_ID_CARD", "BANK_CARD", "EMAIL_ADDRESS", "IP_ADDRESS", "SECRET", "ADDRESS"))
