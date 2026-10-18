"""One finding of any scanner, in CredData's coordinates, plus the parsers that turn each scanner's output into it.

Lines are 1-based and inclusive; col_start is 0-based on line_start, col_end exclusive on line_end (CredData's
ValueStart / ValueEnd convention); None means the whole line. `file` is relative to the CredData checkout
(data/<repo>/...), whatever path the scanner printed.
"""
from __future__ import annotations

import bisect
import dataclasses
import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

# The line convention used everywhere (split_lines and offsets_to_hit); change it only here.
_SEP = re.compile(r"\r?\n")


@dataclass(frozen=True)
class Hit:
    tool: str
    file: str
    line_start: int
    line_end: int
    col_start: int | None
    col_end: int | None
    kind: str    # Hermie entity (SECRET, PERSON, ..., HELD, DETECTOR_ERROR) or the tool's rule name


def write_hits(path: Path, hits: Iterable[Hit]) -> None:
    with open(path, "a", encoding="utf-8") as f:
        for h in hits:
            f.write(json.dumps(dataclasses.asdict(h)) + "\n")


def read_hits(path: Path) -> list[Hit]:
    with open(path, encoding="utf-8") as f:
        return [Hit(**json.loads(line)) for line in f if line.strip()]


def read_text(path: Path) -> tuple[str, bool]:
    data = Path(path).read_bytes()
    try:
        return data.decode("utf-8"), False
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace"), True


def split_lines(text: str) -> list[str]:
    return _SEP.split(text)


def offsets_to_hit(tool: str, file: str, text: str, start: int, end: int, kind: str) -> Hit:
    starts = [0] + [m.end() for m in _SEP.finditer(text)]
    ls = bisect.bisect_right(starts, start) - 1
    le = bisect.bisect_right(starts, max(start, end - 1)) - 1 if end > start else ls
    # an end that falls right after a line's last character stays on that line (col_end = its length)
    return Hit(tool, file, ls + 1, le + 1, start - starts[ls], end - starts[le], kind)


def locate(tool: str, file: str, lines: list[str], line: int, secret: str | None, kind: str) -> tuple[Hit, bool]:
    if secret and 1 <= line <= len(lines):
        i = lines[line - 1].find(secret)
        if i >= 0:
            return Hit(tool, file, line, line, i, i + len(secret), kind), True
    return Hit(tool, file, line, line, None, None, kind), False


def _rel(path: str, cred_root: Path) -> str:
    p = str(path)
    root = str(Path(cred_root).resolve())
    if os.path.isabs(p):
        p = os.path.relpath(os.path.realpath(p), root)
    return os.path.normpath(p).replace(os.sep, "/")


def _load(raw):
    return json.loads(raw) if isinstance(raw, str) else raw


class _Lines:
    """Lines of dataset files, read once each."""

    def __init__(self, cred_root: Path):
        self.root, self.cache = Path(cred_root), {}

    def __call__(self, rel: str) -> list[str]:
        if rel not in self.cache:
            p = self.root / rel
            self.cache[rel] = split_lines(read_text(p)[0]) if p.is_file() else []
        return self.cache[rel]


def parse_gitleaks(raw, cred_root: Path) -> tuple[list[Hit], int]:
    # gitleaks' StartColumn is not reliable, so the column comes from finding Secret on the reported line
    lines, out, fallbacks = _Lines(cred_root), [], 0
    for f in _load(raw) or []:
        rel = _rel(f["File"], cred_root)
        if f["StartLine"] == f["EndLine"]:
            h, found = locate("gitleaks", rel, lines(rel), f["StartLine"], f.get("Secret"), f["RuleID"])
            fallbacks += not found
        else:
            h = Hit("gitleaks", rel, f["StartLine"], f["EndLine"], None, None, f["RuleID"])
        out.append(h)
    return out, fallbacks


def parse_trufflehog(raw: str, cred_root: Path) -> tuple[list[Hit], int]:
    lines, out, fallbacks = _Lines(cred_root), [], 0
    for line in raw.splitlines():
        if not line.strip().startswith("{"):
            continue
        r = json.loads(line)
        fs = (r.get("SourceMetadata") or {}).get("Data", {}).get("Filesystem")
        if not fs:
            continue
        rel = _rel(fs["file"], cred_root)
        h, found = locate("trufflehog", rel, lines(rel), int(fs.get("line") or 1), r.get("Raw"), r["DetectorName"])
        fallbacks += not found
        out.append(h)
    return out, fallbacks


def parse_detect_secrets(raw, cred_root: Path) -> tuple[list[Hit], int]:
    out = []
    for path, items in (_load(raw).get("results") or {}).items():
        rel = _rel(path, cred_root)
        for it in items:
            n = it["line_number"]
            out.append(Hit("detect_secrets", rel, n, n, None, None, it["type"]))
    return out, 0


def parse_credsweeper(raw, cred_root: Path) -> tuple[list[Hit], int]:
    out = []
    for c in _load(raw) or []:
        for d in c["line_data_list"]:
            n, vs, ve = d["line_num"], d.get("value_start"), d.get("value_end")
            cols = (vs, ve) if isinstance(vs, int) and isinstance(ve, int) and 0 <= vs < ve else (None, None)
            out.append(Hit("credsweeper", _rel(d["path"], cred_root), n, n, *cols, c["rule"]))
    return out, 0
