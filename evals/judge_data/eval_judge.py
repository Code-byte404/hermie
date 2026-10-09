"""Score a judge backend on dev / test and compare backends against the adoption criteria.

    python evals/judge_data/eval_judge.py score --backend ollama:gemma4:e4b-mlx --split dev --out scores/ollama-dev.json
    python evals/judge_data/eval_judge.py score --backend laya:aac6fef/laya-mlx --split dev --out scores/laya-dev.json   # needs laya-mlx
    python evals/judge_data/eval_judge.py report --split dev --baseline scores/ollama-dev.json --candidate scores/laya-dev.json [--anchors ...]

`score` writes {id: probability-of-sensitive, ...} plus timing. `report` needs only the standard library.
Long texts: --window N (characters; default 4000) with --long truncate|chunk-max (laya only; ollama sees the first 8000 characters).
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from taxonomy import QUESTION, QUESTION_ID  # noqa: E402

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent


# ----------------------------------------------------------------------------- metrics (pure)

def confusion(scores: list[float], labels: list[bool], threshold: float) -> dict:
    tp = sum(s >= threshold and y for s, y in zip(scores, labels))
    fp = sum(s >= threshold and not y for s, y in zip(scores, labels))
    pos = sum(labels)
    neg = len(labels) - pos
    return {"tp": tp, "fp": fp, "fn": pos - tp, "tn": neg - fp,
            "recall": tp / pos if pos else float("nan"), "fpr": fp / neg if neg else float("nan")}


def auc(scores: list[float], labels: list[bool]) -> float:
    pos = [s for s, y in zip(scores, labels) if y]
    neg = [s for s, y in zip(scores, labels) if not y]
    if not pos or not neg:
        return float("nan")
    ranked = sorted(range(len(scores)), key=lambda i: scores[i])
    ranks = [0.0] * len(scores)
    i = 0
    while i < len(ranked):                       # average ranks over ties
        j = i
        while j + 1 < len(ranked) and scores[ranked[j + 1]] == scores[ranked[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[ranked[k]] = (i + j) / 2 + 1
        i = j + 1
    r_pos = sum(r for r, y in zip(ranks, labels) if y)
    return (r_pos - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def choose_threshold(scores: list[float], labels: list[bool], max_fpr: float) -> float:
    """The threshold with the highest recall whose false-alarm rate on these data is at most max_fpr (ties: the higher threshold)."""
    best_t, best_recall = 1.0 + 1e-9, -1.0
    for t in sorted(set(scores), reverse=True):
        c = confusion(scores, labels, t)
        if c["fpr"] <= max_fpr and c["recall"] > best_recall:
            best_t, best_recall = t, c["recall"]
    return best_t


def bootstrap_ci(scores: list[float], labels: list[bool], threshold: float, key: str, n: int = 1000, seed: int = 3) -> tuple[float, float]:
    rng = random.Random(seed)
    idx = list(range(len(labels)))
    vals = []
    for _ in range(n):
        pick = [rng.choice(idx) for _ in idx]
        v = confusion([scores[i] for i in pick], [labels[i] for i in pick], threshold)[key]
        if v == v:
            vals.append(v)
    vals.sort()
    return vals[int(0.025 * len(vals))], vals[int(0.975 * len(vals)) - 1]


def slices(rows: list[dict], scores: list[float], threshold: float, field: str) -> dict:
    groups: dict[str, list[int]] = defaultdict(list)
    for i, r in enumerate(rows):
        groups[str(r.get(field))].append(i)
    out = {}
    for g, ix in sorted(groups.items()):
        c = confusion([scores[i] for i in ix], [bool(rows[i]["label"]) for i in ix], threshold)
        out[g] = {"n": len(ix), "recall": c["recall"], "fpr": c["fpr"]}
    return out


def adoption(base: dict, cand: dict, base_ms: float, cand_ms: float, anchors_ok: bool | None) -> dict:
    """The four criteria from the plan. base / cand: {"recall", "fpr", "by_category": {cat: recall}} at their operating points."""
    floors = {}
    for cat, r in base["by_category"].items():
        if r == r and r > 0:
            floors[cat] = cand["by_category"].get(cat, 0.0) >= 0.7 * r
    checks = {
        "recall within 2 points of baseline": cand["recall"] >= base["recall"] - 0.02,
        "no category below 70% of baseline recall": all(floors.values()) if floors else False,
        "false alarms at most 1 point above baseline": cand["fpr"] <= base["fpr"] + 0.01,
        "p50 latency at most one fifth of baseline": cand_ms <= base_ms / 5,
    }
    if anchors_ok is not None:
        checks["anchor cases not worse than baseline"] = anchors_ok
    return {"checks": checks, "adopt": all(checks.values()), "failed_categories": [c for c, ok in floors.items() if not ok]}


# ----------------------------------------------------------------------------- backends

def _chunks(text: str, window: int) -> list[str]:
    return [text[i:i + window] for i in range(0, len(text), window)] or [""]


def make_backend(spec: str, window: int, long: str):
    kind, _, arg = spec.partition(":")
    if kind == "ollama":
        sys.path.insert(0, str(ROOT))
        from hermie.config import Config
        from hermie.gate.judge import ContextualJudge
        judge = ContextualJudge(Config(judge=f"ollama:{arg}", judge_timeout_s=120))
        judge.probability("warm up")
        return judge.probability
    if kind == "laya":
        import warnings
        warnings.filterwarnings("ignore")
        import laya_mlx as laya
        agent = laya.load(arg)
        questions = {QUESTION_ID: {"type": "noul", "instructions": QUESTION}}
        agent.predict("warm up", questions)

        def one(text: str) -> float:
            return float(agent.predict(text, questions)["answers"][QUESTION_ID]["noul"])

        def score(text: str) -> float:
            if len(text) <= window or long == "truncate":
                return one(text[:window])
            return max(one(c) for c in _chunks(text, window))
        return score
    raise SystemExit(f"unknown backend {spec!r}; use ollama:MODEL or laya:ID_OR_PATH")


def cmd_score(a) -> int:
    rows = [json.loads(l) for l in (HERE / "data" / f"{a.split}.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    if a.cases:                                   # extra file of {"text", "sensitive"} cases, e.g. the hand-written anchors
        rows = [{"id": f"a{i}", "text": c["text"], "label": c["sensitive"]} for i, c in enumerate(
            json.loads(l) for l in Path(a.cases).read_text(encoding="utf-8").splitlines() if l.strip())]
    score = make_backend(a.backend, a.window, a.long)
    out, ms = {}, []
    for r in rows:
        t = time.perf_counter()
        out[r["id"]] = score(r["text"])
        ms.append((time.perf_counter() - t) * 1000)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps({"backend": a.backend, "split": a.split, "window": a.window, "long": a.long,
                                       "p50_ms": statistics.median(ms), "p95_ms": sorted(ms)[int(0.95 * len(ms)) - 1],
                                       "scores": out}))
    print(f"{a.backend}: {len(out)} texts, p50 {statistics.median(ms):.0f} ms")
    return 0


# ----------------------------------------------------------------------------- report

def _operating(rows, scores, threshold):
    labels = [bool(r["label"]) for r in rows]
    c = confusion(scores, labels, threshold)
    hard = [i for i, r in enumerate(rows) if r.get("role") == "hardneg"]
    cats = slices([r for r in rows if r["label"]], [s for r, s in zip(rows, scores) if r["label"]], threshold, "category")
    return {"threshold": threshold, "recall": c["recall"], "fpr": c["fpr"], "auc": auc(scores, labels),
            "hard_fpr": (sum(scores[i] >= threshold for i in hard) / len(hard)) if hard else float("nan"),
            "by_category": {k: v["recall"] for k, v in cats.items()}, "confusion": c}


def cmd_report(a) -> int:
    rows = [json.loads(l) for l in (HERE / "data" / f"{a.split}.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    rows = [r for r in rows if not r.get("borderline")]
    base = json.loads(Path(a.baseline).read_text())
    cand = json.loads(Path(a.candidate).read_text())
    rows = [r for r in rows if r["id"] in base["scores"] and r["id"] in cand["scores"]]
    bs = [base["scores"][r["id"]] for r in rows]
    cs = [cand["scores"][r["id"]] for r in rows]
    labels = [bool(r["label"]) for r in rows]
    b_op = _operating(rows, bs, a.baseline_threshold)
    if a.candidate_threshold is not None:
        c_t = a.candidate_threshold
    else:
        c_t = choose_threshold(cs, labels, max_fpr=b_op["fpr"])
        print(f"candidate threshold chosen on this split for fpr <= {b_op['fpr']:.3f}: {c_t:.4f}"
              + ("  (do not do this on the test split: pass --candidate-threshold from dev)" if a.split == "test" else ""))
    c_op = _operating(rows, cs, c_t)
    lo, hi = bootstrap_ci(cs, labels, c_t, "recall")
    blo, bhi = bootstrap_ci(bs, labels, a.baseline_threshold, "recall")
    verdict = adoption(b_op, c_op, base["p50_ms"], cand["p50_ms"], None)
    print(f"\n{a.split}: {len(rows)} rows, {sum(labels)} positives (borderline rows excluded)\n")
    print(f"{'':12}{'recall':>9}{'95% CI':>16}{'fpr':>8}{'hard fpr':>10}{'AUC':>7}{'p50 ms':>8}")
    print(f"{'baseline':12}{b_op['recall']:>9.3f}{f'[{blo:.2f},{bhi:.2f}]':>16}{b_op['fpr']:>8.3f}{b_op['hard_fpr']:>10.3f}{b_op['auc']:>7.2f}{base['p50_ms']:>8.0f}")
    print(f"{'candidate':12}{c_op['recall']:>9.3f}{f'[{lo:.2f},{hi:.2f}]':>16}{c_op['fpr']:>8.3f}{c_op['hard_fpr']:>10.3f}{c_op['auc']:>7.2f}{cand['p50_ms']:>8.0f}")
    print("\nrecall by category (baseline / candidate):")
    for cat in sorted(b_op["by_category"]):
        print(f"  {cat:22}{b_op['by_category'][cat]:>6.2f} / {c_op['by_category'].get(cat, float('nan')):.2f}")
    for field in ("form", "length"):
        print(f"\nby {field} (candidate recall / fpr):")
        for k, v in slices(rows, cs, c_t, field).items():
            print(f"  {k:18} n={v['n']:<5} recall {v['recall']:.2f}  fpr {v['fpr']:.2f}")
    print("\nadoption criteria:")
    for k, ok in verdict["checks"].items():
        print(f"  [{'x' if ok else ' '}] {k}")
    print(f"=> {'ADOPT' if verdict['adopt'] else 'DO NOT ADOPT YET'}" + (f" (categories below floor: {verdict['failed_categories']})" if verdict["failed_categories"] else ""))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("score")
    sp.add_argument("--backend", required=True)
    sp.add_argument("--split", choices=["train", "dev", "test"], default="dev")
    sp.add_argument("--cases", help="score a {text, sensitive} JSONL file instead of a split")
    sp.add_argument("--out", required=True)
    sp.add_argument("--window", type=int, default=4000)
    sp.add_argument("--long", choices=["truncate", "chunk-max"], default="truncate")
    sp = sub.add_parser("report")
    sp.add_argument("--split", choices=["dev", "test"], default="dev")
    sp.add_argument("--baseline", required=True)
    sp.add_argument("--candidate", required=True)
    sp.add_argument("--baseline-threshold", type=float, default=0.5)
    sp.add_argument("--candidate-threshold", type=float)
    a = p.parse_args(argv)
    return cmd_score(a) if a.cmd == "score" else cmd_report(a)


if __name__ == "__main__":
    sys.exit(main())
