"""Second-pass labelling by an independent labeller that sees only the text and the question.

    python evals/judge_data/label.py prepare [--splits dev test] [--train-sample 0.25]   # writes label/in/NN.json + prompts
    python evals/judge_data/label.py collect                                              # reads label/out/NN.json, writes data/label_report.json and marks borderline rows
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from taxonomy import QUESTION  # noqa: E402

HERE = Path(__file__).resolve().parent
BATCH_CHARS = 40_000

PROMPT = """You are a careful annotator. For each numbered text in {inp}, answer this question with true or false:

  {question}

Answer true only if the text itself contains such information (a specific person's private details, or non-public company
information). Answer false for general discussion, public information, ordinary code or logs, invented generic examples, and text
where people are only placeholders like <PERSON_1>. Judge the content, nothing else. Do not guess what the author intended.

Write {out} as a JSON object mapping each item id to true or false, for every id in the input file, using a short Python script
(json.dump). Then reply with one line: the path and the number of answers. Do not paste the texts or your reasoning.
"""


def prepare(splits, train_sample: float, seed: int = 7) -> int:
    data = HERE / "data"
    items = []
    rng = random.Random(seed)
    for name in splits:
        for line in (data / f"{name}.jsonl").read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if name == "train" and rng.random() >= train_sample:
                continue
            items.append({"id": r["id"], "text": r["text"]})
    rng.shuffle(items)
    (HERE / "label" / "in").mkdir(parents=True, exist_ok=True)
    (HERE / "label" / "out").mkdir(parents=True, exist_ok=True)
    (HERE / "label" / "prompts").mkdir(parents=True, exist_ok=True)
    batches, cur, size = [], [], 0
    for it in items:
        if cur and size + len(it["text"]) > BATCH_CHARS:
            batches.append(cur)
            cur, size = [], 0
        cur.append(it)
        size += len(it["text"])
    if cur:
        batches.append(cur)
    for i, b in enumerate(batches, 1):
        inp = HERE / "label" / "in" / f"{i:03d}.json"
        out = HERE / "label" / "out" / f"{i:03d}.json"
        inp.write_text(json.dumps(b, ensure_ascii=False, indent=1))
        (HERE / "label" / "prompts" / f"{i:03d}.md").write_text(PROMPT.format(inp=inp, out=out, question=QUESTION))
    print(f"{len(items)} items in {len(batches)} labelling batches")
    return 0


def collect() -> int:
    data = HERE / "data"
    answers: dict[str, bool] = {}
    for p in sorted((HERE / "label" / "out").glob("*.json")):
        answers.update(json.loads(p.read_text()))
    rows = {}
    for name in ("train", "dev", "test"):
        for line in (data / f"{name}.jsonl").read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                rows[r["id"]] = (name, r)
    checked = {i: a for i, a in answers.items() if i in rows}
    disagree = [i for i, a in checked.items() if bool(a) != rows[i][1]["label"]]
    by_split: dict[str, dict] = {}
    for i, (name, r) in rows.items():
        s = by_split.setdefault(name, {"rows": 0, "checked": 0, "disagree": 0, "pos_to_neg": 0, "neg_to_pos": 0})
        s["rows"] += 1
        if i in checked:
            s["checked"] += 1
            if i in disagree:
                s["disagree"] += 1
                s["pos_to_neg" if r["label"] else "neg_to_pos"] += 1
    report = {"by_split": by_split, "disagree_ids": disagree}
    (data / "label_report.json").write_text(json.dumps(report, indent=1))
    # mark borderline in place
    dis = set(disagree)
    for name in ("train", "dev", "test"):
        path = data / f"{name}.jsonl"
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                r["borderline"] = r["id"] in dis
                out.append(json.dumps(r, ensure_ascii=False))
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(json.dumps(by_split))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("prepare")
    sp.add_argument("--splits", nargs="+", default=["dev", "test"])
    sp.add_argument("--train-sample", type=float, default=0.25)
    sub.add_parser("collect")
    a = p.parse_args(argv)
    if a.cmd == "prepare":
        return prepare([s for s in a.splits] + (["train"] if a.train_sample > 0 else []), a.train_sample)
    return collect()


if __name__ == "__main__":
    sys.exit(main())
