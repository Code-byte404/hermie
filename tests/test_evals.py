"""The eval set doubles as a regression test: what the rules layer can catch must be caught, cases labeled
normal must not be flagged; routing cases are well-formed; the threshold sweep runs."""
import json
import sys
from pathlib import Path

import pytest

from hermie.config import Settings
from hermie.policy import Route
from hermie.privacy import PrivacyGate

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from evals.run_evals import load_cases, privacy_metrics, sweep  # noqa: E402

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


def test_routing_cases_well_formed():
    cases = load_cases(EVALS / "routing_cases.jsonl")
    assert len(cases) >= 30
    valid = {r.value for r in Route}
    for c in cases:
        assert c["task"].strip() and c["expect"] and set(c["expect"]) <= valid, c


def test_sweep_runs_on_recorded_signals():
    rec = {"task": "x", "expect": ["plan"], "route": "plan", "signals": {
        "privacy": {"sensitive": False}, "task_type": {"choice": "planning", "confidence": 1.0},
        "task_probs": {"planning": 1.0}, "complexity": {"score": 2, "confidence": 1.0}, "complexity_probs": [0, 0, 1],
        "routellm_win_rate": 0.8, "needs_workspace": True, "needs_workspace_prob": 0.67}}
    rec2 = json.loads(json.dumps(rec)) | {"expect": ["cloud"]}
    rec2["signals"]["needs_workspace_prob"] = 0.33
    res = sweep([rec, rec2], {"min_confidence": [0.6, 0.9], "routellm_threshold": [0.5],
                              "needs_workspace_threshold": [0.3, 0.5]})
    assert len(res) == 4 and res[0][0] == 1.0 and res[0][1]["needs_workspace_threshold"] == 0.5


def test_review_stats_groups_by_task():
    from evals.run_evals import review_stats
    recs = [{"task": "a", "round": 1, "passed": True, "problems": []},
            {"task": "b", "round": 1, "passed": False, "problems": ["out.csv does not exist"]},
            {"task": "b", "round": 2, "passed": True, "problems": []},
            {"task": "c", "round": 1, "passed": False, "problems": ["the tests were not run"]},
            {"task": "c", "round": 2, "passed": False, "problems": ["the tests were still not run", "section two is missing content"]}]
    m = review_stats(recs)
    assert (m["tasks"], m["first_pass"], m["fixed"], m["still_failing"]) == (3, 1, 1, 1)
    assert m["problems"] == {"unverified": 2, "missing file": 1, "content mismatch": 1}
    assert dict(m["rounds_used"]) == {1: 1, 2: 2}
