"""Run Hermie's real gate over every CredData file named in meta/, as the proxy would see a file the agent read:
the whole text as one tool-result string, default config, a fresh empty mapping per file.

    python -m evals.creddata.run_hermie --cred-root ~/hermie-bench.noindex/CredData --out RESULTS_DIR \
        [--workers 8] [--limit N] [--judge SPEC]

Writes OUT/hermie.jsonl (hits), OUT/hermie_files.jsonl (per-file stats), OUT/hermie_done.txt (finished files) and
OUT/hermie_manifest.json. A rerun resumes after the files in hermie_done.txt.
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import resource
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from evals.creddata.hits import Hit, offsets_to_hit, read_hits, read_text, split_lines, write_hits
from evals.creddata.labels import load_meta
from hermie.config import Config
from hermie.gate.redact import MappingStore
from hermie.gate.types import Origin

TOOL = "hermie"
REPO = Path(__file__).resolve().parents[2]


def scan_file_info(gate_factory: Callable, cred_root: Path, rel: str, tmp: Path) -> tuple[list[Hit], bool, str]:
    """Hits for one file, whether it had invalid UTF-8, and the gate's reason."""
    text, bad = read_text(Path(cred_root) / rel)
    Path(tmp).mkdir(parents=True, exist_ok=True)
    d = Path(tempfile.mkdtemp(dir=tmp))
    try:
        res = gate_factory(d).scan(text, Origin.TOOL, path_hint=None)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    if res.reason.startswith("detector_error") or res.sensitive:
        # nothing of the file would be sent: every line counts as flagged
        kind = "DETECTOR_ERROR" if res.reason.startswith("detector_error") else "HELD"
        lines = split_lines(text)
        n = len(lines) - 1 if len(lines) > 1 and lines[-1] == "" else len(lines)
        return [Hit(TOOL, rel, 1, max(n, 1), None, None, kind)], bad, res.reason
    out = [offsets_to_hit(TOOL, rel, text, f.start, f.end, f.entity) for f in res.findings]
    return out, bad, res.reason


def scan_file(gate_factory: Callable, cred_root: Path, rel: str, tmp: Path) -> list[Hit]:
    return scan_file_info(gate_factory, cred_root, rel, tmp)[0]


def resume_state(out: Path) -> set[str]:
    """Files already finished; hits and stats of unfinished files are dropped so a rerun adds no duplicates."""
    done_path = out / "hermie_done.txt"
    done = set(done_path.read_text().split()) if done_path.exists() else set()
    hits_path = out / "hermie.jsonl"
    if hits_path.exists():
        kept = [h for h in read_hits(hits_path) if h.file in done]
        hits_path.unlink()
        write_hits(hits_path, kept)
    files_path = out / "hermie_files.jsonl"
    if files_path.exists():
        rows = [line for line in files_path.read_text().splitlines() if line and json.loads(line)["file"] in done]
        files_path.write_text("".join(r + "\n" for r in rows))
    return done


# ---------------------------------------------------------------- worker processes

_W: dict = {}


def _init(cred_root: str, judge: str | None, tmp: str) -> None:
    from hermie.gate.gate import Gate
    from hermie.gate.recognizers import build_analyzer
    cfg = Config(data_dir=Path(tmp), judge=judge)
    analyzer = build_analyzer(cfg.languages, cfg.deny_words)
    judge_obj = None
    if judge:
        from hermie.gate.judge import make_judge
        judge_obj = make_judge(cfg)
    _W.update(cred_root=Path(cred_root), tmp=Path(tmp) / str(os.getpid()),
              factory=lambda d: Gate(Config(data_dir=d, judge=judge), analyzer=analyzer, judge=judge_obj,
                                     store=MappingStore(d / "mapping.json")))


def _work(rel: str):
    t = time.monotonic()
    found, bad, reason = scan_file_info(_W["factory"], _W["cred_root"], rel, _W["tmp"])
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_mb = rss / 1024 / 1024 if sys.platform == "darwin" else rss / 1024
    return rel, found, {"file": rel, "seconds": round(time.monotonic() - t, 3), "invalid_utf8": bad,
                        "reason": reason}, os.getpid(), rss_mb


def _git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True).stdout.strip()
    except OSError:
        return ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m evals.creddata.run_hermie")
    ap.add_argument("--cred-root", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--judge")
    a = ap.parse_args(argv)
    cred_root, out = a.cred_root.expanduser().resolve(), a.out.expanduser()
    out.mkdir(parents=True, exist_ok=True)
    files = sorted({r.file for r in load_meta(cred_root / "meta")})
    if a.limit:
        files = files[:a.limit]
    missing = [f for f in files if not (cred_root / f).is_file()]
    done = resume_state(out)
    todo = [f for f in files if f not in done and f not in set(missing)]
    print(f"{len(files)} files, {len(done)} done, {len(missing)} missing, {len(todo)} to scan", flush=True)

    t0, rss = time.monotonic(), {}
    with tempfile.TemporaryDirectory(prefix="hermie-bench-") as tmp, \
            mp.get_context("spawn").Pool(a.workers, _init, (str(cred_root), a.judge, tmp)) as pool, \
            open(out / "hermie_files.jsonl", "a") as stats_f, open(out / "hermie_done.txt", "a") as done_f:
        for n, (rel, found, stats, pid, rss_mb) in enumerate(pool.imap_unordered(_work, todo), 1):
            write_hits(out / "hermie.jsonl", found)
            stats_f.write(json.dumps(stats) + "\n")
            stats_f.flush()
            done_f.write(rel + "\n")
            done_f.flush()
            rss[pid] = max(rss.get(pid, 0), rss_mb)
            if n % 200 == 0 or n == len(todo):
                print(f"{n}/{len(todo)} files, {time.monotonic() - t0:.0f}s", flush=True)

    stats = [json.loads(line) for line in (out / "hermie_files.jsonl").read_text().splitlines() if line]
    prev = json.loads((out / "hermie_manifest.json").read_text()) if (out / "hermie_manifest.json").exists() else {}
    manifest = {
        "git_commit": _git_commit(),
        "config": {"languages": list(Config().languages), "presidio_threshold": Config().presidio_threshold,
                   "judge": a.judge},
        "files": len(stats),
        "files_missing": missing,
        "files_invalid_utf8": sum(s["invalid_utf8"] for s in stats),
        "detector_errors": sum(s["reason"].startswith("detector_error") for s in stats),
        "held": sum(s["reason"] in ("judge",) or s["reason"].startswith("smuggling") for s in stats),
        "seconds": round(prev.get("seconds", 0) + time.monotonic() - t0, 1),
        "per_worker_peak_rss_mb": sorted((round(v) for v in rss.values()), reverse=True),
        "bytes_scanned": sum((cred_root / s["file"]).stat().st_size for s in stats),
        "slowest": [[s["file"], s["seconds"]] for s in sorted(stats, key=lambda s: -s["seconds"])[:20]],
    }
    (out / "hermie_manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    print(json.dumps({k: v for k, v in manifest.items() if k != "slowest"}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
