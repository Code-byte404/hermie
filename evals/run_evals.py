"""Calibration tool: measure the privacy gate on labeled cases.

    python evals/run_evals.py privacy                 # rules layer (Presidio): recall / false alarms, a few seconds, no Ollama needed
    python evals/run_evals.py privacy --judge         # plus the local judge's contextual check (needs Ollama)

Case files are JSONL:
    privacy_cases.jsonl: {"text", "sensitive", "layer": rules|ner|judge, "entities": [...], "known_fp"/"known_miss"}

Real requests can be appended to both files directly (note: privacy cases go to the judge model but never leave the machine).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hermie.config import Settings  # noqa: E402

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
    from hermie.privacy import PrivacyGate
    s = Settings()
    judge = None
    if args.judge:
        from hermie.judge import OllamaJudge
        judge = OllamaJudge(s)
    gate = PrivacyGate(s, judge=judge)
    cases = load_cases(Path(args.cases))
    t0 = time.time()
    verdicts = [gate.check(c["text"], use_judge=args.judge) for c in cases]
    m = privacy_metrics(cases, verdicts, with_judge=args.judge)
    print(f"Privacy gate · {'rules layer + judge' if args.judge else 'rules layer only'} · {len(cases)} cases · {time.time() - t0:.1f}s")
    print(f"  precision={m['precision']:.2f}  recall={m['recall']:.2f}  "
          f"tp={m['tp']} fp={m['fp']} fn={m['fn']} tn={m['tn']}")
    print("  recall per entity: " + "  ".join(f"{k} {hit}/{n}" for k, (hit, n) in sorted(m["per_entity"].items())))
    if m["misses"]:
        print("  misses:")
        for c in m["misses"]:
            print(f"    - [{c.get('layer')}] {c['text'][:60]!r}")
    if m["false_alarms"]:
        print("  false alarms:")
        for c, reason in m["false_alarms"]:
            print(f"    - {c['text'][:60]!r} -> {reason}")
    if m["known"]:
        print(f"  known trade-offs (excluded from the metrics): {len(m['known'])}")
    if args.judge:
        probs = [(c, v.contextual_prob) for c, v in zip(cases, verdicts) if v.contextual_prob is not None]
        print("  contextual probability distribution (for tuning CONTEXT_PRIVACY_THRESHOLD):")
        for c, p in sorted(probs, key=lambda x: -x[1]):
            print(f"    {p:.2f}  {'sensitive' if c['sensitive'] else 'normal'}  {c['text'][:50]!r}")



# ====================================================================== entry point

def main(argv=None) -> None:
    p = argparse.ArgumentParser(description="Calibration tool for the privacy gate and the router")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("privacy")
    a.add_argument("--judge", action="store_true", help="also run the local judge model (needs Ollama)")
    a.add_argument("--cases", default=str(HERE / "privacy_cases.jsonl"))
    a.set_defaults(fn=run_privacy)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
