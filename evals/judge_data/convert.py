"""Convert a split to Laya's / Unsloth's training rows: {state, questions, expected}.

    python evals/judge_data/convert.py train [--out PATH]    # borderline rows are left out
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from taxonomy import QUESTION, QUESTION_ID  # noqa: E402

HERE = Path(__file__).resolve().parent
QUESTIONS = {QUESTION_ID: {"type": "noul", "instructions": QUESTION}}


def to_laya(row: dict) -> dict:
    return {"state": row["text"], "questions": QUESTIONS, "expected": {QUESTION_ID: bool(row["label"])}}


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("split", choices=["train", "dev", "test"])
    p.add_argument("--out")
    a = p.parse_args(argv)
    out = Path(a.out) if a.out else HERE / "data" / f"{a.split}.laya.jsonl"
    n = 0
    with open(out, "w", encoding="utf-8") as f:
        for line in (HERE / "data" / f"{a.split}.jsonl").read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("borderline"):
                continue
            f.write(json.dumps(to_laya(r), ensure_ascii=False) + "\n")
            n += 1
    print(f"{n} rows -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
