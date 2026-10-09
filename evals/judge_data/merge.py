"""Validate raw generation output, drop what breaks the rules, dedupe, and write data/all.jsonl.

    python evals/judge_data/merge.py            # reads raw/*.jsonl, plan/batches.jsonl; writes data/all.jsonl + data/merge_report.json
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from taxonomy import FORMS, LENGTH_TOLERANCE, LENGTHS  # noqa: E402

HERE = Path(__file__).resolve().parent
ROLES = {"pos": True, "hardneg": False, "easyneg": False}
_CJK = re.compile(r"[　-〿぀-ヿ㐀-䶿一-鿿가-힯＀-￯]")
# Anything shaped like a real credential is rejected; the fake-key convention (sk-test-...) is allowed.
_KEYS = re.compile(r"(?<![A-Za-z0-9])(?:sk-(?!test)[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{30,}|"
                   r"xox[abprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{35}|[sr]k_live_[A-Za-z0-9]{16,}|-----BEGIN [A-Z ]*PRIVATE KEY)")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_OK_DOMAINS = ("example.com", "example.org", "example.net")   # and their subdomains (RFC 2606 reserved)


def _ok_domain(d: str) -> bool:
    d = d.lower()
    return any(d == ok or d.endswith("." + ok) for ok in _OK_DOMAINS)
_PHONE = re.compile(r"(?<![\d-])(?:\+?1[ .-]?)?\(?(\d{3})\)?[ .-]?(\d{3})[ .-]?(\d{4})(?![\d-])")


def problems(item: dict, batch: dict) -> list[str]:
    """Why an item must be dropped (empty list: it passes). Pure function of the item and its batch."""
    out = []
    text = item.get("text")
    if not isinstance(text, str) or not text.strip():
        return ["empty"]
    if item.get("role") not in ROLES:
        out.append("bad_role")
    if item.get("form") not in FORMS:
        out.append("bad_form")
    if not isinstance(item.get("scenario"), str) or not item["scenario"]:
        out.append("no_scenario")
    low, high = LENGTHS[batch["length"]]
    if not (low * LENGTH_TOLERANCE[0] <= len(text) <= high * LENGTH_TOLERANCE[1]):
        out.append("length")
    if _CJK.search(text):
        out.append("cjk")
    if _KEYS.search(text):
        out.append("real_key_shape")
    if any(not _ok_domain(m.group(1)) for m in _EMAIL.finditer(text)):
        out.append("real_email_domain")
    for m in _PHONE.finditer(text):
        if not (m.group(1) == "555" and m.group(2).startswith("01")):
            out.append("real_phone_shape")
            break
    return out


def _shingles(text: str, k: int = 5) -> set[int]:
    words = re.findall(r"\w+", text.lower())
    if len(words) < k:
        return {hash(" ".join(words))}
    return {hash(" ".join(words[i:i + k])) for i in range(len(words) - k + 1)}


def near_duplicates(texts: list[str], threshold: float = 0.7, groups: list[str] | None = None) -> set[int]:
    """Indices to drop: of each group of near-duplicates (Jaccard of 5-word shingles at or above threshold) the first is kept.
    `groups` gives each text's scenario: texts of the same scenario are matched pairs and are never compared with each other."""
    sh = [_shingles(t) for t in texts]
    index: dict[int, list[int]] = defaultdict(list)
    drop: set[int] = set()
    for i, s in enumerate(sh):
        if i in drop:
            continue
        seen: Counter = Counter()
        for h in s:
            for j in index.get(h, ()):
                seen[j] += 1
        dup = False
        for j, c in seen.items():
            if j in drop or (groups is not None and groups[j] == groups[i]):
                continue
            if c / (len(s) + len(sh[j]) - c) >= threshold:
                dup = True
                break
        if dup:
            drop.add(i)
            continue
        for h in s:
            index[h].append(i)
    return drop


def load_batches(plan: Path) -> dict[str, dict]:
    return {b["id"]: b for b in (json.loads(l) for l in plan.read_text().splitlines() if l.strip())}


def merge(raw_dir: Path, batches: dict[str, dict]) -> tuple[list[dict], dict]:
    rows: list[dict] = []
    report = {"files": 0, "items": 0, "dropped": Counter(), "incomplete_scenarios": 0, "duplicates": 0, "bad_lines": 0}
    for path in sorted(raw_dir.glob("*.jsonl")):
        batch = batches.get(path.stem)
        if batch is None:
            continue
        report["files"] += 1
        items = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                items.append(json.loads(line))
            except ValueError:
                report["bad_lines"] += 1
        report["items"] += len(items)
        kept = []
        for it in items:
            probs = problems(it, batch)
            if probs:
                for p in probs:
                    report["dropped"][p] += 1
            else:
                kept.append(it)
        if batch["kind"] in ("pair", "xl"):   # a scenario needs both of its texts
            by_sc: dict[str, list[dict]] = defaultdict(list)
            for it in kept:
                by_sc[it["scenario"]].append(it)
            good = []
            for sc, group in by_sc.items():
                if sorted(g["role"] for g in group) == ["hardneg", "pos"]:
                    good += group
                else:
                    report["incomplete_scenarios"] += 1
            kept = good
        for it in kept:
            sid = f"{batch['id']}:{it['scenario']}"
            rows.append({"scenario": sid, "label": ROLES[it["role"]], "role": it["role"], "category": batch["category"],
                         "form": it["form"], "origin": FORMS[it["form"]][0], "length": batch["length"], "kind": batch["kind"],
                         "split_only": batch.get("split_only"), "style": batch.get("style", "synthetic"), "text": it["text"]})
    texts = [r["text"] for r in rows]
    drop = near_duplicates(texts, groups=[r["scenario"] for r in rows])
    report["duplicates"] = len(drop)
    rows = [r for i, r in enumerate(rows) if i not in drop]
    # a scenario that lost one text to the dedupe loses the other too
    for kind in ("pair", "xl"):
        by_sc = defaultdict(list)
        for r in rows:
            if r["kind"] == kind:
                by_sc[r["scenario"]].append(r)
        broken = {sc for sc, g in by_sc.items() if len(g) != 2}
        report["incomplete_scenarios"] += len(broken)
        rows = [r for r in rows if r["scenario"] not in broken]
    for r in rows:
        r["id"] = hashlib.sha1(r["text"].encode()).hexdigest()[:12]
    report["dropped"] = dict(report["dropped"])
    report["rows"] = len(rows)
    report["positives"] = sum(r["label"] for r in rows)
    return rows, report


def main(argv=None) -> int:
    batches = load_batches(HERE / "plan" / "batches.jsonl")
    rows, report = merge(HERE / "raw", batches)
    (HERE / "data").mkdir(exist_ok=True)
    with open(HERE / "data" / "all.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    (HERE / "data" / "merge_report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
