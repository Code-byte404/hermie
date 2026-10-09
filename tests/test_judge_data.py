"""The judge data pipeline: validation, dedupe, pairing, splitting, conversion and the scoring maths."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals" / "judge_data"))

import merge  # noqa: E402

BATCH = {"id": "pair-hr-s-1", "kind": "pair", "category": "hr", "length": "s", "split_only": None}


def _item(**kw):
    base = {"scenario": "s01", "role": "pos", "form": "chat_prompt", "text": "Priya Nair from the support team is being let go on Friday; keep it quiet until HR announces."}
    return base | kw


def test_a_clean_item_passes():
    assert merge.problems(_item(), BATCH) == []


def test_validation_rejects_rule_breakers():
    cases = {
        "empty": _item(text="  "),
        "bad_role": _item(role="maybe"),
        "bad_form": _item(form="poem"),
        "cjk": _item(text="Priya Nair 被解雇了, support team lead, final day is Friday, keep it quiet please."),
        "real_key_shape": _item(text="Priya Nair is out. deploy key AKIAIOSFODNN7EXAMPLE stays with ops until the layoff is announced."),
        "real_email_domain": _item(text="Priya Nair (priya.nair@acmecorp.io) is being let go on Friday; keep it quiet until HR announces."),
        "real_phone_shape": _item(text="Priya Nair is being let go on Friday, reach her at 415-867-5309 before HR announces it."),
        "length": _item(text="too short"),
    }
    for name, it in cases.items():
        assert name in merge.problems(it, BATCH), name


def test_fake_data_conventions_are_allowed():
    ok = _item(text="Priya Nair (priya.nair@example.com, 555-0142) is being let go; key sk-test-demo-not-a-real-key-0123456789 is rotated; HR announces Friday.")
    assert merge.problems(ok, BATCH) == []


def test_near_duplicates_keep_the_first():
    a = "The quarterly layoff list for the support team includes Priya Nair and Tom Reyes and will be announced on Friday by HR."
    texts = [a, a.replace("Friday", "Monday"), "Completely different text about build output and a failing unit test in the parser module."]
    assert merge.near_duplicates(texts) == {1}


def test_merge_requires_both_texts_of_a_pair_and_assigns_ids(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    pos = _item()
    neg = _item(role="hardneg", text="Our HR handbook explains how layoffs are announced: managers are briefed first, then the whole team hears together on one day.")
    orphan = _item(scenario="s02", text="Tom Reyes in finance is being let go on Monday, do not tell the team before HR does it.")
    (raw / "pair-hr-s-1.jsonl").write_text("\n".join(json.dumps(x) for x in (pos, neg, orphan)) + "\n")
    rows, report = merge.merge(raw, {"pair-hr-s-1": BATCH})
    assert [r["label"] for r in rows] == [True, False] and report["incomplete_scenarios"] == 1
    assert all(r["scenario"] == "pair-hr-s-1:s01" and len(r["id"]) == 12 and r["origin"] == "user" for r in rows)


import convert  # noqa: E402
import split  # noqa: E402


def _rows(n_scen=40):
    rows = []
    for i in range(n_scen):
        for role, label in (("pos", True), ("hardneg", False)):
            rows.append({"id": f"{i}{role}", "scenario": f"b:{i}", "label": label, "role": role, "category": "hr",
                         "kind": "pair", "split_only": None, "text": f"text {i} {role}"})
    rows.append({"id": "xl1", "scenario": "xl:1", "label": True, "role": "pos", "category": "mixed", "kind": "xl", "split_only": "test", "text": "x"})
    return rows


def test_split_keeps_scenarios_together_and_is_deterministic():
    a = split.split_rows(_rows())
    b = split.split_rows(_rows())
    assert {k: [r["id"] for r in v] for k, v in a.items()} == {k: [r["id"] for r in v] for k, v in b.items()}
    where = {}
    for name, rs in a.items():
        for r in rs:
            assert where.setdefault(r["scenario"], name) == name
    assert len(a["test"]) >= 2 * round(40 * 0.15) + 1 and len(a["dev"]) == 2 * round(40 * 0.12)
    assert sum(len(v) for v in a.values()) == 81 and any(r["id"] == "xl1" for r in a["test"])


def test_convert_matches_the_laya_row_schema():
    row = convert.to_laya({"text": "Priya is being let go", "label": True})
    assert row == {"state": "Priya is being let go",
                   "questions": {"private": {"type": "noul", "instructions": row["questions"]["private"]["instructions"]}},
                   "expected": {"private": True}}
    assert row["questions"]["private"]["instructions"].startswith("Does this text contain private information")


import eval_judge as ev  # noqa: E402


def test_confusion_auc_and_threshold_choice():
    scores = [0.9, 0.8, 0.4, 0.3, 0.2, 0.1]
    labels = [True, True, False, True, False, False]
    c = ev.confusion(scores, labels, 0.5)
    assert (c["tp"], c["fp"], c["fn"], c["tn"]) == (2, 0, 1, 3)
    assert ev.auc(scores, labels) == (3 * 3 - 0 - 1) / 9      # one positive (0.3) below one negative (0.4)
    assert ev.auc([0.5, 0.5], [True, False]) == 0.5           # ties
    t = ev.choose_threshold(scores, labels, max_fpr=0.0)
    assert ev.confusion(scores, labels, t)["fp"] == 0 and ev.confusion(scores, labels, t)["tp"] == 2
    assert ev.choose_threshold(scores, labels, max_fpr=0.34) == 0.3


def test_adoption_checks_all_four_criteria():
    base = {"recall": 0.80, "fpr": 0.05, "by_category": {"hr": 0.8, "legal": 0.8}}
    good = {"recall": 0.79, "fpr": 0.05, "by_category": {"hr": 0.7, "legal": 0.8}}
    assert ev.adoption(base, good, 280, 25, None)["adopt"]
    assert not ev.adoption(base, good | {"recall": 0.7}, 280, 25, None)["adopt"]
    assert not ev.adoption(base, good | {"by_category": {"hr": 0.3, "legal": 0.8}}, 280, 25, None)["adopt"]
    assert not ev.adoption(base, good | {"fpr": 0.2}, 280, 25, None)["adopt"]
    assert not ev.adoption(base, good, 280, 100, None)["adopt"]


def test_matched_pairs_are_not_duplicates_of_each_other_but_copies_across_scenarios_are():
    long_a = " ".join(f"routine line number {i} of the build log shows nothing unusual" for i in range(60))
    long_b = long_a + " plus a private line about Priya Nair being let go on Friday"
    texts = [long_a, long_b, long_a + " "]
    assert merge.near_duplicates(texts) == {1, 2}
    assert merge.near_duplicates(texts, groups=["s1", "s1", "s2"]) == {2}


def test_email_domains_example_and_its_subdomains_pass_but_example_edu_does_not():
    mk = lambda addr: _item(text=f"Priya Nair ({addr}) is being let go on Friday; keep it quiet until HR announces it to the team.")
    assert merge.problems(mk("p@acme.example.com"), BATCH) == []
    assert merge.problems(mk("p@lab.example.org"), BATCH) == []
    assert "real_email_domain" in merge.problems(mk("p@example.edu"), BATCH)
    assert "real_email_domain" in merge.problems(mk("p@notexample.com"), BATCH)
