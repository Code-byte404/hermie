"""Split data/all.jsonl by scenario into train / dev / test, stratified, deterministic.

    python evals/judge_data/split.py            # writes data/{train,dev,test}.jsonl; refuses to overwrite a frozen test set
"""
from __future__ import annotations

import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
FRACTIONS = (("test", 0.15), ("dev", 0.12))      # the rest is train
SEED = "hermie-judge-1"


def _rank(scenario: str) -> float:
    return int(hashlib.sha256(f"{SEED}:{scenario}".encode()).hexdigest()[:12], 16) / 16 ** 12


def split_rows(rows: list[dict]) -> dict[str, list[dict]]:
    """Scenarios never span splits. Strata are (kind, category) so every category appears in dev and test.
    Rows marked split_only (the 8,000-character items) go to that split only."""
    out: dict[str, list[dict]] = {"train": [], "dev": [], "test": []}
    strata: dict[tuple, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if r.get("split_only"):
            out[r["split_only"]].append(r)
        else:
            strata[(r["kind"], r["category"])][r["scenario"]].append(r)
    for scenarios in strata.values():
        order = sorted(scenarios, key=_rank)
        n = len(order)
        n_test = round(n * FRACTIONS[0][1])
        n_dev = round(n * FRACTIONS[1][1])
        for i, sc in enumerate(order):
            name = "test" if i < n_test else "dev" if i < n_test + n_dev else "train"
            out[name] += scenarios[sc]
    return out


def main(argv=None) -> int:
    data = HERE / "data"
    rows = [json.loads(l) for l in (data / "all.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    frozen = data / "test.FROZEN"
    if frozen.exists():
        print("test set is frozen; delete data/test.FROZEN on purpose to rebuild it", file=sys.stderr)
        return 1
    parts = split_rows(rows)
    for name, rs in parts.items():
        with open(data / f"{name}.jsonl", "w", encoding="utf-8") as f:
            for r in rs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    summary = {n: {"rows": len(rs), "positives": sum(r["label"] for r in rs)} for n, rs in parts.items()}
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
