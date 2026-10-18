"""Score every scanner's hits against CredData's labelled rows and write the results.

    python -m evals.creddata.score --cred-root ~/hermie-bench.noindex/CredData --run RESULTS_DIR

Reads RUN/hermie.jsonl and RUN/<tool>.jsonl; writes RUN/summary.md, RUN/dev_misses.md, RUN/dev_false_alarms.md and
RUN/manual_sample.csv. The rules are the ones in evals/creddata/README.md.
"""
from __future__ import annotations

import argparse
import csv
import functools
import io
import math
import random
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from evals.creddata.hits import Hit, read_hits, read_text, split_lines
from evals.creddata.labels import Row, in_scope, load_meta, load_split, split_of
from evals.creddata.run_tools import TOOLS

HEADLINE_KINDS = frozenset({"SECRET", "HELD", "DETECTOR_ERROR"})
WHOLE_FILE_KINDS = frozenset({"HELD", "DETECTOR_ERROR"})
SEED = 20261017
SAMPLE_SIZE = {"hermie": 100}   # every other tool: 25
SAMPLE_DEFAULT = 25
WIDE = 50   # hits spanning more lines than this are checked against every row of their file

BIASES = """\
- Lines were labelled from CredSweeper's (and a few other tools') candidates. A line no tool flagged is not
  labelled. So a flag on an unlabelled line is "unknown", not a false positive, and recall is measured only against
  secrets some tool once found.
- CredSweeper's ML model is trained on CredData. Its numbers are inflated by train/test overlap; it is the reference
  ceiling, not a fair peer.
- Values are obfuscated: random strings with the original character set and length, example values for fixed
  formats (for example AWS keys), example files for keys and certificates. Detection by shape still works;
  provider checksums (if any detector relies on them) may not.
- Source code only. Agent traffic (prompts, logs, tool output) is not covered here."""


# ---------------------------------------------------------------- matching

def _hit_span(h: Hit, line: int) -> tuple[float, float]:
    s = h.col_start if line == h.line_start and h.col_start is not None else 0
    e = h.col_end if line == h.line_end and h.col_end is not None else math.inf
    return s, e


def matches(row: Row, hit: Hit, line_only: bool = False) -> bool:
    if hit.file != row.file or hit.line_end < row.line_start or hit.line_start > row.line_end:
        return False
    if (line_only or row.line_start != row.line_end or row.value_start is None or row.value_end is None
            or hit.col_start is None):
        return True
    s, e = _hit_span(hit, row.line_start)
    return s < row.value_end and row.value_start < e


def _value_spans(row: Row, lines: list[str]) -> list[tuple[int, int, int]]:
    """(line, start, end) of the labelled value on each of its lines."""
    out = []
    for n in range(row.line_start, row.line_end + 1):
        length = len(lines[n - 1]) if n <= len(lines) else 0
        s = row.value_start if n == row.line_start and row.value_start is not None else 0
        e = row.value_end if n == row.line_end and row.value_end is not None else length
        if e > s:
            out.append((n, s, e))
    return out


def covered(row: Row, hits: list[Hit], lines: list[str]) -> str:
    """covered / partial / missed: is the value inside the union of the hits' spans (Hermie's placeholders)?"""
    spans = _value_spans(row, lines)
    if not spans:
        spans = [(row.line_start, 0, 1)]
    any_overlap, all_covered = False, True
    for n, s, e in spans:
        ivs = sorted(_hit_span(h, n) for h in hits if h.file == row.file and h.line_start <= n <= h.line_end)
        pos = s
        for a, b in ivs:
            if a < e and s < b:
                any_overlap = True
            if a <= pos:
                pos = max(pos, b)
        if pos < e:
            all_covered = False
    return "covered" if all_covered and any_overlap else "partial" if any_overlap else "missed"


class _Index:
    """Hits by file and line, so each row only looks at the hits on its own lines."""

    def __init__(self, hits: list[Hit]):
        self.by_line: dict[tuple[str, int], list[Hit]] = defaultdict(list)
        self.wide: dict[str, list[Hit]] = defaultdict(list)
        for h in hits:
            if h.line_end - h.line_start > WIDE:
                self.wide[h.file].append(h)
            else:
                for n in range(h.line_start, h.line_end + 1):
                    self.by_line[(h.file, n)].append(h)

    def near(self, row: Row) -> list[Hit]:
        out = list(self.wide.get(row.file, ()))
        for n in range(row.line_start, row.line_end + 1):
            out += self.by_line.get((row.file, n), ())
        return out


def flags(rows: list[Row], hits: list[Hit], line_only: bool = False) -> list[bool]:
    idx = _Index(hits)
    return [any(matches(r, h, line_only) for h in idx.near(r)) for r in rows]


# ---------------------------------------------------------------- counting

@dataclass
class Counts:
    tp: int = 0
    fn: int = 0
    fp: int = 0
    x: int = 0

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0

    @property
    def precision_x_as_fp(self) -> float:
        d = self.tp + self.fp + self.x
        return self.tp / d if d else 0.0

    def add(self, row: Row, flagged: bool) -> None:
        if row.label == "T":
            if flagged:
                self.tp += 1
            else:
                self.fn += 1
        elif flagged:
            if row.label == "X":
                self.x += 1
            else:
                self.fp += 1


_SPLIT = None


def _split(repo: str) -> str:
    global _SPLIT
    if _SPLIT is None:
        try:
            _SPLIT = load_split()
        except OSError:
            _SPLIT = {}
    return _SPLIT.get(repo) or split_of(repo)


def _keep(row: Row, split: str | None, scope: bool) -> bool:
    return (split is None or _split(row.repo) == split) and (not scope or in_scope(row))


def score(rows: list[Row], hits: list[Hit], *, split: str | None, scope: bool, line_only: bool,
          flagged: list[bool] | None = None) -> Counts:
    """`flagged` (from `flags(rows, hits, line_only)`) saves recomputing the matches for several splits."""
    if flagged is None:
        flagged = flags(rows, hits, line_only)
    c = Counts()
    for r, f in zip(rows, flagged):
        if _keep(r, split, scope):
            c.add(r, f)
    return c


def per_category(rows: list[Row], hits: list[Hit], split: str | None,
                 flagged: list[bool] | None = None) -> dict[str, Counts]:
    if flagged is None:
        flagged = flags(rows, hits)
    out: dict[str, Counts] = defaultdict(Counts)
    for r, f in zip(rows, flagged):
        if _keep(r, split, False):
            for cat in r.categories:
                out[cat].add(r, f)
    return dict(out)


def unlabelled(rows: list[Row], hits: list[Hit]) -> list[tuple[str, int]]:
    labelled = {(r.file, n) for r in rows for n in range(r.line_start, r.line_end + 1)}
    found = {(h.file, n) for h in hits if h.kind not in WHOLE_FILE_KINDS
             for n in range(h.line_start, h.line_end + 1)}
    return sorted(found - labelled)


def missing_files(rows: list[Row], cred_root: Path) -> set[str]:
    return {f for f in {r.file for r in rows} if not (Path(cred_root) / f).is_file()}


# ---------------------------------------------------------------- report

def _file_split(file: str) -> str:
    return _split(file.split("/")[1])


@functools.lru_cache(maxsize=512)
def _lines(path: str) -> list[str]:
    p = Path(path)
    return split_lines(read_text(p)[0]) if p.is_file() else []


def _line_text(cred_root: Path, file: str, n: int) -> str:
    lines = _lines(str(Path(cred_root) / file))
    text = lines[n - 1] if 0 < n <= len(lines) else ""
    return text.strip()[:300]


def _fmt(c: Counts) -> str:
    return (f"{c.tp} | {c.fn} | {c.fp} | {c.x} | {c.precision:.3f} | {c.recall:.3f} | {c.f1:.3f} | "
            f"{c.precision_x_as_fp:.3f}")


TABLE_HEAD = ("| tool | split | TP | FN | FP | X flagged | precision | recall | F1 | precision (X as FP) |\n"
              "|---|---|---|---|---|---|---|---|---|---|\n")


def _sample(tool: str, lines: list[tuple[str, int]], tool_hits: list[Hit], cred_root: Path) -> list[list[str]]:
    pool = sorted(fl for fl in lines if _file_split(fl[0]) == "test")
    k = min(SAMPLE_SIZE.get(tool, SAMPLE_DEFAULT), len(pool))
    picked = random.Random(f"{SEED}:{tool}").sample(pool, k)
    kinds: dict[tuple[str, int], set[str]] = defaultdict(set)
    want = set(picked)
    for h in tool_hits:
        for n in range(h.line_start, min(h.line_end, h.line_start + WIDE) + 1):
            if (h.file, n) in want:
                kinds[(h.file, n)].add(h.kind)
    return [[tool, f, str(n), _line_text(cred_root, f, n), ";".join(sorted(kinds[(f, n)])), "", "", ""]
            for f, n in sorted(picked)]


def _row_list(title: str, items: list[tuple[Row, list[str]]], cred_root: Path) -> str:
    by_cat: dict[str, list] = defaultdict(list)
    for r, kinds in items:
        by_cat[":".join(r.categories) or "(none)"].append((r, kinds))
    out = [f"# {title}\n", f"{len(items)} rows.\n"]
    for cat, rs in sorted(by_cat.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        out.append(f"\n## {cat} ({len(rs)})\n")
        for r, kinds in sorted(rs, key=lambda x: (x[0].file, x[0].line_start)):
            fired = ", ".join(sorted(kinds)) or "nothing"
            out.append(f"- `{r.file}:{r.line_start}` [{r.label}] fired: {fired}\n"
                       f"  `{_line_text(cred_root, r.file, r.line_start).replace('`', chr(39))}`\n")
    return "".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m evals.creddata.score")
    ap.add_argument("--cred-root", type=Path, required=True)
    ap.add_argument("--run", type=Path, required=True)
    a = ap.parse_args(argv)
    cred_root, run = a.cred_root.expanduser().resolve(), a.run.expanduser().resolve()
    all_rows = load_meta(cred_root / "meta")
    missing = missing_files(all_rows, cred_root)
    rows = [r for r in all_rows if r.file not in missing]

    tool_hits = {t: read_hits(run / f"{t}.jsonl") for t in ("hermie", *TOOLS) if (run / f"{t}.jsonl").exists()}
    variants: dict[str, list[Hit]] = {}
    for t, hs in tool_hits.items():
        if t == "hermie":
            variants["hermie"] = [h for h in hs if h.kind in HEADLINE_KINDS]
            variants["hermie (all kinds)"] = hs
        else:
            variants[t] = hs
    flagged = {v: flags(rows, hs) for v, hs in variants.items()}
    flagged_line = {v: flags(rows, hs, line_only=True) for v, hs in variants.items()}

    md = io.StringIO()
    w = md.write
    w("# CredData benchmark\n\n")
    labels_by_split = Counter((_split(r.repo), r.label) for r in rows)
    top = Counter(r.repo for r in rows if r.label == "T").most_common(1)
    w(f"Rows scored: {len(rows)} ({dict(sorted(labels_by_split.items()))}). Largest repo by T rows: "
      f"{top[0][0] if top else '-'} ({top[0][1] if top else 0} T, split {_split(top[0][0]) if top else '-'}).\n\n")
    if missing:
        w(f"Missing data files, their rows excluded: {len(missing)}\n\n" + "".join(f"- {f}\n" for f in sorted(missing))
          + "\n")
    for title, scope, fl in (("Headline: every category, column overlap where both sides have columns", False, flagged),
                             ("Hermie's scope (out-of-scope categories dropped)", True, flagged),
                             ("Line overlap only (every tool)", False, flagged_line)):
        w(f"## {title}\n\n" + TABLE_HEAD)
        for v in variants:
            for split in ("test", "dev", None):
                c = score(rows, variants[v], split=split, scope=scope, line_only=False, flagged=fl[v])
                w(f"| {v} | {split or 'all'} | {_fmt(c)} |\n")
        w("\n")

    for split in ("test", None):
        cats = {v: per_category(rows, variants[v], split, flagged[v]) for v in variants}
        n_t = Counter(c for r in rows if r.label == "T" and _keep(r, split, False) for c in r.categories)
        w(f"## Recall per category ({split or 'all'} split, categories with 20+ T rows)\n\n")
        w("| category | T rows | " + " | ".join(variants) + " |\n|---|---|" + "---|" * len(variants) + "\n")
        for cat, n in n_t.most_common():
            if n < 20:
                break
            w(f"| {cat} | {n} | " + " | ".join(f"{cats[v][cat].recall:.2f}" for v in variants) + " |\n")
        w("\n")

    unl = {v: unlabelled(rows, variants[v]) for v in variants}
    w("## Flags on unlabelled lines (unknown, not false positives)\n\n| tool | lines | test split |\n|---|---|---|\n")
    for v in variants:
        w(f"| {v} | {len(unl[v])} | {sum(1 for f, _ in unl[v] if _file_split(f) == 'test')} |\n")
    w("\n")

    if "hermie" in tool_hits:
        hs = tool_hits["hermie"]
        by_file: dict[str, list[Hit]] = defaultdict(list)
        for h in hs:
            by_file[h.file].append(h)
        w("## Hermie: is the value covered by placeholders (T rows, every finding)\n\n"
          "| split | covered | partial | missed |\n|---|---|---|---|\n")
        cov = {s: Counter() for s in ("test", "dev")}
        for r in sorted((r for r in rows if r.label == "T"), key=lambda r: r.file):
            lines = _lines(str(cred_root / r.file))
            cov[_split(r.repo)][covered(r, by_file.get(r.file, []), lines)] += 1
        for s in ("test", "dev"):
            w(f"| {s} | {cov[s]['covered']} | {cov[s]['partial']} | {cov[s]['missed']} |\n")
        tot = cov["test"] + cov["dev"]
        w(f"| all | {tot['covered']} | {tot['partial']} | {tot['missed']} |\n\n")

        idx = _Index(hs)
        on_t = Counter(k for r in rows if r.label == "T"
                       for k in {h.kind for h in idx.near(r) if matches(r, h)})
        unl_all = set(unl["hermie (all kinds)"])
        on_unl = Counter(h.kind for h in hs if h.kind not in WHOLE_FILE_KINDS and (h.file, h.line_start) in unl_all)
        w("## Hermie: which entities fire\n\n| entity | T rows hit | hits on unlabelled lines |\n|---|---|---|\n")
        for k in sorted(set(on_t) | set(on_unl), key=lambda k: -(on_t[k] + on_unl[k])):
            w(f"| {k} | {on_t[k]} | {on_unl[k]} |\n")
        w("\n")

        dev_rows = [(i, r) for i, r in enumerate(rows) if _split(r.repo) == "dev"]

        def fired(r: Row) -> list[str]:
            return sorted({h.kind for h in idx.near(r) if matches(r, h)})

        misses = [(r, fired(r)) for i, r in dev_rows if r.label == "T" and not flagged["hermie"][i]]
        alarms = [(r, fired(r)) for i, r in dev_rows if r.label != "T" and flagged["hermie"][i]]
        (run / "dev_misses.md").write_text(_row_list("Hermie misses (dev split, T rows not flagged)", misses,
                                                     cred_root))
        (run / "dev_false_alarms.md").write_text(_row_list("Hermie false alarms (dev split, F / X rows flagged)",
                                                           alarms, cred_root))

    w("## Known biases\n\n" + BIASES + "\n")
    (run / "summary.md").write_text(md.getvalue())

    buf = io.StringIO()
    cw = csv.writer(buf, lineterminator="\n")
    cw.writerow(["tool", "file", "line", "line_text", "kinds", "claude_label", "claude_reason", "user_label"])
    for v in variants:
        if v == "hermie (all kinds)":
            continue
        for rec in _sample(v, unl[v], variants[v], cred_root):
            cw.writerow(rec)
    (run / "manual_sample.csv").write_text(buf.getvalue())
    print(f"wrote {run / 'summary.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
