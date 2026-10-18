"""CredData labels: the rows of meta/*.csv, the fixed dev / test split by repo, and Hermie's scope.

    python -m evals.creddata.labels --meta ~/hermie-bench.noindex/CredData/meta [--write-split]
"""
from __future__ import annotations

import argparse
import csv
import json
import zlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

SPLIT_PATH = Path(__file__).resolve().parent / "split.json"

# Categories Hermie does not set out to catch; a row is out of scope only if all its categories are here.
OUT_OF_SCOPE = frozenset({"UUID", "AWS S3 Bucket", "Firebase Domain", "Tencent WeChat API App ID", "Nonce", "Salt"})


@dataclass(frozen=True)
class Row:
    id: int
    repo: str
    file: str            # relative to the CredData checkout: data/<repo>/<scope>/<id><ext>
    line_start: int      # 1-based, inclusive
    line_end: int
    label: str           # T, F or X
    value_start: int | None   # column on line_start, 0-based; None = whole line
    value_end: int | None     # column on line_end, exclusive
    categories: tuple[str, ...]


def _col(v: str) -> int | None:
    if not v.strip():
        return None
    n = int(v)
    return None if n < 0 else n


def load_meta(meta_dir: Path) -> list[Row]:
    rows = []
    for path in sorted(Path(meta_dir).glob("*.csv")):
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows.append(Row(int(r["Id"]), r["RepoName"], r["FilePath"], int(r["LineStart"]), int(r["LineEnd"]),
                                r["GroundTruth"], _col(r["ValueStart"]), _col(r["ValueEnd"]),
                                tuple(c for c in r["Category"].split(":") if c)))
    return rows


def split_of(repo: str) -> str:
    return "test" if zlib.crc32(repo.encode()) % 2 == 1 else "dev"


def in_scope(row: Row) -> bool:
    return not row.categories or not set(row.categories) <= OUT_OF_SCOPE


def load_split(path: Path = SPLIT_PATH) -> dict[str, str]:
    return json.loads(Path(path).read_text())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m evals.creddata.labels")
    ap.add_argument("--meta", type=Path, required=True)
    ap.add_argument("--write-split", action="store_true")
    a = ap.parse_args(argv)
    rows = load_meta(a.meta)
    repos = sorted({r.repo for r in rows} | {p.stem for p in a.meta.glob("*.csv")})
    print(f"rows {len(rows)}, repos {len(repos)}")
    print("labels", dict(Counter(r.label for r in rows)))
    print("by split", dict(Counter((split_of(r.repo), r.label) for r in rows)))
    print("T in scope", sum(1 for r in rows if r.label == "T" and in_scope(r)))
    cats = Counter(c for r in rows if r.label == "T" for c in r.categories)
    print("top T categories", cats.most_common(15))
    if a.write_split:
        SPLIT_PATH.write_text(json.dumps({r: split_of(r) for r in repos}, indent=1, sort_keys=True) + "\n")
        print(f"wrote {SPLIT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
