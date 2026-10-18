"""Fast loop for tuning the rules on the dev split only: the real gate with a stub analyzer (no Presidio model), so
the rules layer is what runs, scored against CredData's dev rows. Test-split files are never read.

    python -m evals.creddata.quick --cred-root ~/hermie-bench.noindex/CredData --out DIR [--workers 8]

Writes OUT/quick.jsonl (hits), OUT/quick_flags.json (row id -> flagged), OUT/quick_misses.md,
OUT/quick_false_alarms.md and appends a line to OUT/quick_history.tsv. When OUT already holds a quick_flags.json,
prints what changed and writes OUT/quick_delta.md (rows gained, lost, new false alarms, false alarms gone).
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import tempfile
import time
from collections import defaultdict
from pathlib import Path

from evals.creddata import score as S
from evals.creddata.hits import Hit, write_hits
from evals.creddata.labels import Row, load_meta
from evals.creddata.run_hermie import _git_commit, scan_file_info
from hermie.config import Config
from hermie.gate.redact import MappingStore

SPLIT = "dev"


class NoModel:
    """Stands in for the Presidio analyzer: finds nothing, so only `rules.find` contributes."""

    def analyze(self, text, language, score_threshold=None, **kw):
        return []


def _factory(d: Path):
    from hermie.gate.gate import Gate
    return Gate(Config(data_dir=d), analyzer=NoModel(), store=MappingStore(d / "mapping.json"))


_W: dict = {}


def _init(cred_root: str, tmp: str) -> None:
    _W.update(cred_root=Path(cred_root), tmp=Path(tmp))


def _work(rel: str) -> list[Hit]:
    return scan_file_info(_factory, _W["cred_root"], rel, _W["tmp"])[0]


def scan_dev(cred_root: Path, files: list[str], workers: int) -> list[Hit]:
    out: list[Hit] = []
    with tempfile.TemporaryDirectory(prefix="hermie-quick-") as tmp:
        if workers <= 1:
            _init(str(cred_root), tmp)
            for rel in files:
                out += _work(rel)
        else:
            with mp.get_context("spawn").Pool(workers, _init, (str(cred_root), tmp)) as pool:
                for found in pool.imap_unordered(_work, files, chunksize=4):
                    out += found
    return out


def _delta(rows: list[Row], before: dict[str, bool], now: dict[str, bool]) -> dict[str, list[Row]]:
    d: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        b, n = before.get(str(r.id)), now[str(r.id)]
        if b is None or b == n:
            continue
        if r.label == "T":
            d["gained" if n else "lost"].append(r)
        else:
            d["new false alarms" if n else "false alarms gone"].append(r)
    return d


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m evals.creddata.quick")
    ap.add_argument("--cred-root", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args(argv)
    cred_root, out = a.cred_root.expanduser().resolve(), a.out.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    rows = [r for r in load_meta(cred_root / "meta") if S._split(r.repo) == SPLIT and (cred_root / r.file).is_file()]
    files = sorted({r.file for r in rows})

    t0 = time.monotonic()
    hits = [h for h in scan_dev(cred_root, files, a.workers) if h.kind in S.HEADLINE_KINDS]
    seconds = time.monotonic() - t0
    (out / "quick.jsonl").unlink(missing_ok=True)
    write_hits(out / "quick.jsonl", hits)

    flagged = S.flags(rows, hits)
    now = {str(r.id): f for r, f in zip(rows, flagged)}
    allc = S.score(rows, hits, split=SPLIT, scope=False, line_only=False, flagged=flagged)
    scope = S.score(rows, hits, split=SPLIT, scope=True, line_only=False, flagged=flagged)
    lines = [f"{len(files)} dev files in {seconds:.0f}s",
             f"dev all:   P {allc.precision:.3f}  R {allc.recall:.3f}  F1 {allc.f1:.3f}  (TP {allc.tp} FN {allc.fn} "
             f"FP {allc.fp} X {allc.x})",
             f"dev scope: P {scope.precision:.3f}  R {scope.recall:.3f}  F1 {scope.f1:.3f}",
             "", f"{'category':<34} {'T':>6} {'recall':>7} {'FP':>6}"]
    cats = S.per_category(rows, hits, SPLIT, flagged=flagged)
    for cat, c in sorted(cats.items(), key=lambda kv: -(kv[1].tp + kv[1].fn))[:25]:
        lines.append(f"{cat:<34} {c.tp + c.fn:>6} {c.recall:>7.2f} {c.fp + c.x:>6}")

    flags_path = out / "quick_flags.json"
    if flags_path.exists():
        d = _delta(rows, json.loads(flags_path.read_text()), now)
        lines += ["", "change since last run: " + ", ".join(f"{k} {len(d.get(k, []))}" for k in
                                                           ("gained", "lost", "new false alarms", "false alarms gone"))]
        (out / "quick_delta.md").write_text("".join(
            S._row_list(f"{k} (dev)", [(r, []) for r in d.get(k, [])], cred_root)
            for k in ("gained", "lost", "new false alarms", "false alarms gone")))
    flags_path.write_text(json.dumps(now))
    misses = [(r, []) for r, f in zip(rows, flagged) if r.label == "T" and not f]
    alarms = [(r, ["SECRET"]) for r, f in zip(rows, flagged) if r.label != "T" and f]
    (out / "quick_misses.md").write_text(S._row_list("Rules misses (dev split)", misses, cred_root))
    (out / "quick_false_alarms.md").write_text(S._row_list("Rules false alarms (dev split)", alarms, cred_root))
    with open(out / "quick_history.tsv", "a") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')}\t{_git_commit()[:7]}\t{allc.precision:.4f}\t"
                f"{allc.recall:.4f}\t{allc.f1:.4f}\t{scope.precision:.4f}\t{scope.recall:.4f}\t{scope.f1:.4f}\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
