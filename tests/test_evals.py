"""The eval set doubles as a regression test: what the rules layer can catch must be caught, cases labeled
normal must not be flagged."""
import sys
from pathlib import Path

import pytest

from hermie.config import Settings
from hermie.privacy import PrivacyGate

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from evals.run_evals import load_cases, privacy_metrics  # noqa: E402

EVALS = Path(__file__).resolve().parent.parent / "evals"


@pytest.fixture(scope="module")
def privacy_cases():
    return load_cases(EVALS / "privacy_cases.jsonl")


def test_privacy_rules_layer_recall_and_precision(privacy_cases, analyzer):
    gate = PrivacyGate(Settings(), judge=None, analyzer=analyzer)
    verdicts = [gate.check(c["text"], use_judge=False) for c in privacy_cases]
    m = privacy_metrics(privacy_cases, verdicts, with_judge=False)
    rule_misses = [c for c in m["misses"] if c.get("layer") == "rules"]
    assert not rule_misses, [c["text"] for c in rule_misses]
    assert not m["false_alarms"], [(c["text"], r) for c, r in m["false_alarms"]]
    for e in ("CN_MOBILE", "CN_ID_CARD", "BANK_CARD", "EMAIL_ADDRESS", "IP_ADDRESS", "SECRET"):
        hit, n = m["per_entity"][e]
        assert n > 0 and hit == n, (e, hit, n)
