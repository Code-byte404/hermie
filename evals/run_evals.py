"""Calibration tool: measure the privacy gate and the router on labeled cases, and sweep thresholds offline.

    python evals/run_evals.py privacy                 # rules layer (Presidio): recall / false alarms, a few seconds, no Ollama needed
    python evals/run_evals.py privacy --judge         # plus the local judge's contextual check (needs Ollama)
    python evals/run_evals.py routing                 # run the routing cases with the real judge + RouteLLM, record signals to evals/signals.jsonl
    python evals/run_evals.py routing --no-routellm --out my_signals.jsonl
    python evals/run_evals.py sweep evals/signals.jsonl   # sweep thresholds offline over the recorded signals (pure function, instant)
    python evals/run_evals.py review [-v]             # stats over local review records: first-round pass rate, fixed-after-review rate, problem types

Case files are JSONL:
    privacy_cases.jsonl: {"text", "sensitive", "layer": rules|ner|judge, "entities": [...], "known_fp"/"known_miss"}
    routing_cases.jsonl: {"task", "expect": [acceptable routes...], "kind"}

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
from hermie.policy import Force  # noqa: E402
# signals replay and the threshold sweep live in hermie.calibrate (also used by hermie --calibrate); re-exported here
from hermie.calibrate import GRID, signals_from_record, sweep  # noqa: E402,F401

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


# ====================================================================== routing

async def run_routing_async(args) -> None:
    from hermie.complexity import RouteLLMScorer
    from hermie.judge import OllamaJudge
    from hermie.privacy import PrivacyGate
    from hermie.router import EntryRouter
    s = Settings()
    judge = OllamaJudge(s)
    gate = PrivacyGate(s, judge=judge)
    scorer = RouteLLMScorer(s.routellm_checkpoint) if (s.routellm_enabled and not args.no_routellm) else None
    router = EntryRouter(s, judge, gate, scorer)
    cases = load_cases(Path(args.cases))
    out = Path(args.out)
    records = []
    hits = 0
    print(f"Routing · {len(cases)} cases · judge={s.judge_model} samples={s.judge_samples} routellm={'on' if scorer else 'off'}")
    with open(out, "w", encoding="utf-8") as f:
        for i, c in enumerate(cases, 1):
            t0 = time.time()
            r = await router.route(c["task"], c["task"], Force.NONE)
            dt = time.time() - t0
            route = r.decision.route.value
            ok = route in c["expect"]
            hits += ok
            rec = {"task": c["task"], "expect": c["expect"], "kind": c.get("kind"), "route": route,
                   "reasons": r.decision.reasons, "signals": r.signals_dict(), "latency_s": round(dt, 1)}
            records.append(rec)
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            print(f"  {'✓' if ok else '✗'} {i:>2} {route:<12} {dt:5.1f}s  {c['task'][:44]}"
                  + ("" if ok else f"   expected {c['expect']}"))
    print(f"Hits {hits}/{len(cases)} = {hits / len(cases):.2f}; signals recorded to {out}, "
          f"use the sweep subcommand to tune thresholds offline")
    print_confusion(records)


def print_confusion(records: list[dict]) -> None:
    table: dict[str, Counter] = defaultdict(Counter)
    for r in records:
        table[r.get("kind") or "?"][r["route"]] += 1
    routes = ["local", "local_verify", "plan", "cloud"]
    print(f"  {'kind':<22}" + "".join(f"{x:>13}" for x in routes))
    for kind, cnt in sorted(table.items()):
        print(f"  {kind:<22}" + "".join(f"{cnt.get(x, 0):>13}" for x in routes))


def run_routing(args) -> None:
    asyncio.run(run_routing_async(args))


# ====================================================================== sweep

def run_sweep(args) -> None:
    records = load_cases(Path(args.signals))
    records = [r for r in records if "signals" in r and "task_type" in r["signals"]]
    if not records:
        sys.exit("No usable records in the signals file (records where the judge failed have no task_type)")
    results = sweep(records, GRID)
    s = Settings()
    current = {"min_confidence": s.min_confidence, "routellm_threshold": s.routellm_threshold,
               "needs_workspace_threshold": s.needs_workspace_threshold}
    cur_acc = next((acc for acc, cfg in results if cfg == current), None)
    print(f"Threshold sweep · {len(records)} records · {len(results)} combinations")
    print(f"  current config {current} -> {cur_acc:.2f}" if cur_acc is not None else f"  current config {current} is not in the grid")
    print("  top 10:")
    for acc, cfg in results[:10]:
        print(f"    {acc:.2f}  MIN_CONFIDENCE={cfg['min_confidence']}  ROUTELLM_THRESHOLD={cfg['routellm_threshold']}"
              f"  needs_workspace>{cfg['needs_workspace_threshold']}")
    print("  Note: with fewer than 100 cases the differences are mostly noise; collect real requests before tuning.")


# ====================================================================== review: stats over review records

REVIEW_CATEGORIES = [
    ("unverified", ("verif", "test", "not run", "never ran", "did not run", "was not run")),
    ("missing file", ("does not exist", "missing file", "not generated", "was not created", "not created", "no such file")),
    ("content mismatch", ("mismatch", "does not match", "missing", "only", "incomplete", "wrong", "incorrect", "format")),
    ("run failure", ("failed", "error", "exception", "exit code")),
]


def categorize_problem(text: str) -> str:
    for name, keys in REVIEW_CATEGORIES:
        if any(k in text for k in keys):
            return name
    return "other"


def review_stats(records: list[dict]) -> dict:
    """Aggregate per task: first-round pass rate, fixed-after-review rate, still failing at the end;
    problems counted by type."""
    by_task: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_task[r.get("task", "?")].append(r)
    first_pass = fixed = still_failing = 0
    problems = Counter()
    for recs in by_task.values():
        recs.sort(key=lambda r: r.get("round", 0))
        if recs[0]["passed"]:
            first_pass += 1
        elif recs[-1]["passed"]:
            fixed += 1
        else:
            still_failing += 1
        for r in recs:
            for p in r.get("problems", []):
                problems[categorize_problem(p)] += 1
    return {"tasks": len(by_task), "reviews": len(records), "first_pass": first_pass, "fixed": fixed,
            "still_failing": still_failing, "problems": dict(problems.most_common()),
            "rounds_used": Counter(len(v) for v in by_task.values())}


def run_review(args) -> None:
    path = Path(args.log).expanduser()
    if not path.exists():
        sys.exit(f"No review records: {path}")
    records = [r for r in load_cases(path) if "passed" in r]
    m = review_stats(records)
    n = m["tasks"] or 1
    print(f"Local review · {m['reviews']} records · {m['tasks']} tasks ({path})")
    print(f"  first-round pass {m['first_pass']}/{n} = {m['first_pass'] / n:.2f}   passed after fixes {m['fixed']}/{n}   "
          f"still failing {m['still_failing']}/{n}")
    print("  rounds used per task: " + "  ".join(f"{k} round(s)×{v}" for k, v in sorted(m["rounds_used"].items())))
    if m["problems"]:
        print("  problem types: " + "  ".join(f"{k} {v}" for k, v in m["problems"].items()))
    if args.verbose:
        for r in records:
            if not r["passed"]:
                print(f"    ✗ [{r.get('task', '?')} r{r.get('round')}] " + "; ".join(r.get("problems", []))[:120])
    print("  How to read this: a high first-round pass rate means the executor prompt is doing its job; many passes "
          "after fixes mean VERIFY_ROUNDS pays off; many still failing with concentrated problem types means the "
          "executor prompt or the reviewer's criteria should change.")


# ====================================================================== entry point

def main(argv=None) -> None:
    p = argparse.ArgumentParser(description="Calibration tool for the privacy gate and the router")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("privacy")
    a.add_argument("--judge", action="store_true", help="also run the local judge model (needs Ollama)")
    a.add_argument("--cases", default=str(HERE / "privacy_cases.jsonl"))
    a.set_defaults(fn=run_privacy)
    b = sub.add_parser("routing")
    b.add_argument("--cases", default=str(HERE / "routing_cases.jsonl"))
    b.add_argument("--out", default=str(HERE / "signals.jsonl"))
    b.add_argument("--no-routellm", action="store_true")
    b.set_defaults(fn=run_routing)
    c = sub.add_parser("sweep")
    c.add_argument("signals")
    c.set_defaults(fn=run_sweep)
    d = sub.add_parser("review", help="stats over local review records (reviews.jsonl)")
    d.add_argument("--log", default="~/.hermie/reviews.jsonl")
    d.add_argument("-v", "--verbose", action="store_true")
    d.set_defaults(fn=run_review)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
