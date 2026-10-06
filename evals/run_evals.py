"""Calibration tool: measure the privacy gate on labeled cases.

    python evals/run_evals.py privacy --languages en --cases evals/privacy_cases_en.jsonl
    python evals/run_evals.py privacy --languages en,zh     # the default case file is the Chinese-engine one

Case files are JSONL:
    privacy_cases.jsonl: {"text", "sensitive", "layer": rules|ner|judge, "entities": [...], "known_fp"/"known_miss"}


"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

HERE = Path(__file__).resolve().parent


def load_cases(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ====================================================================== privacy

def privacy_metrics(cases: list[dict], verdicts: list, *, with_judge: bool) -> dict:
    """Returns printable / assertable metrics. Positives with layer=rules only look at the rules layer;
    positives with layer=judge only count when with_judge is set."""
    tp = fp = fn = tn = 0
    per_entity: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # entity -> [hits, total]
    misses, false_alarms, known = [], [], []
    for c, v in zip(cases, verdicts):
        expected = c["sensitive"]
        layer = c.get("layer")
        if expected and layer == "judge" and not with_judge:
            continue  # not the rules layer's responsibility
        got = v.sensitive
        if (c.get("known_miss") and expected and not got) or (c.get("known_fp") and not expected and got):
            known.append(c)  # known trade-offs: listed separately, excluded from the metrics
            continue
        found = {f.entity for f in v.findings}
        for e in c.get("entities", []):
            per_entity[e][1] += 1
            per_entity[e][0] += e in found
        if expected and got:
            tp += 1
        elif expected and not got:
            fn += 1
            misses.append(c)
        elif not expected and got:
            fp += 1
            false_alarms.append((c, v.reason))
        else:
            tn += 1
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "precision": precision, "recall": recall,
            "per_entity": {k: (v[0], v[1]) for k, v in per_entity.items()},
            "misses": misses, "false_alarms": false_alarms, "known": known}


def run_privacy(args) -> None:
    from hermie.gate.recognizers import build_analyzer, scan
    from types import SimpleNamespace
    langs = tuple(args.languages.split(","))
    analyzer = build_analyzer(langs)
    cases = load_cases(Path(args.cases))
    t0 = time.time()
    verdicts = []
    for c in cases:
        fs = scan(analyzer, c["text"], langs, args.threshold)
        verdicts.append(SimpleNamespace(sensitive=bool(fs), findings=fs,
                                        reason=f"entities: {sorted({f.entity for f in fs})}"))
    m = privacy_metrics(cases, verdicts, with_judge=False)
    print(f"Privacy gate rules layer ({','.join(langs)}) - {len(cases)} cases - {time.time() - t0:.1f}s")
    print(f"  precision={m['precision']:.2f}  recall={m['recall']:.2f}  "
          f"tp={m['tp']} fp={m['fp']} fn={m['fn']} tn={m['tn']}")
    print("  recall per entity: " + "  ".join(f"{k} {hit}/{n}" for k, (hit, n) in sorted(m["per_entity"].items())))
    for c in m["misses"]:
        print(f"  miss: [{c.get('layer')}] {c['text'][:60]!r}")
    for c, reason in m["false_alarms"]:
        print(f"  false alarm: {c['text'][:60]!r} -> {reason}")
    if m["known"]:
        print(f"  known trade-offs (excluded from the metrics): {len(m['known'])}")


# ====================================================================== entry point

def main(argv=None) -> None:
    p = argparse.ArgumentParser(description="Calibration tool for the privacy gate")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("privacy")
    a.add_argument("--languages", default="en,zh")
    a.add_argument("--threshold", type=float, default=0.5)
    a.add_argument("--cases", default=str(HERE / "privacy_cases.jsonl"))
    a.set_defaults(fn=run_privacy)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
