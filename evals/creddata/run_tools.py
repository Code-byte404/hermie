"""Run the other scanners over CredData's data/ (default rules, no verification, no network) and normalize their
output into OUT/<tool>.jsonl hits.

    python -m evals.creddata.run_tools --cred-root ~/hermie-bench.noindex/CredData --out RESULTS_DIR \
        [--only TOOL ...] [--bench-bin DIR]

--bench-bin is the bin dir of the conda env holding detect-secrets and credsweeper (`hermie-bench`).
Each run is added to OUT/tools_manifest.json; a failing tool is recorded and the others still run.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

from evals.creddata import hits as H

TOOLS = ("gitleaks", "trufflehog", "detect_secrets", "credsweeper")
RAW = {"gitleaks": "gitleaks.raw.json", "trufflehog": "trufflehog.raw.jsonl",
       "detect_secrets": "detect_secrets.raw.json", "credsweeper": "credsweeper.raw.json"}
PARSERS = {"gitleaks": H.parse_gitleaks, "trufflehog": H.parse_trufflehog,
           "detect_secrets": H.parse_detect_secrets, "credsweeper": H.parse_credsweeper}
STDOUT_TOOLS = ("trufflehog", "detect_secrets")   # these print their report instead of writing a file


def commands(tool: str, out: Path, bench_bin: Path) -> tuple[list[str], list[str]]:
    """(scan argv, version argv); both run with cwd = the CredData checkout so paths come out as data/..."""
    if tool == "gitleaks":
        return (["gitleaks", "dir", "data", "--report-format", "json", "--report-path", f"{out}/{RAW[tool]}",
                 "--no-banner", "--exit-code", "0"], ["gitleaks", "version"])
    if tool == "trufflehog":
        # every result whatever its verification state, no verification calls, no dedup across files
        return (["trufflehog", "filesystem", "data", "--json", "--no-verification", "--no-update",
                 "--results=verified,unverified,unknown", "--deduplication-cache-size=0"],
                ["trufflehog", "--version"])
    if tool == "detect_secrets":
        return [f"{bench_bin}/detect-secrets", "scan", "--all-files", "data"], [f"{bench_bin}/detect-secrets", "--version"]
    if tool == "credsweeper":
        return ([f"{bench_bin}/credsweeper", "--path", "data", "--save-json", f"{out}/{RAW[tool]}"],
                [f"{bench_bin}/credsweeper", "--version"])
    raise ValueError(tool)


def _run(argv: list[str], cwd: Path, stdout_path: Path | None) -> tuple[int, str, str]:
    """(exit code, stdout when not sent to a file, stderr tail)."""
    try:
        if stdout_path is None:
            p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
            return p.returncode, p.stdout, p.stderr[-2000:]
        with open(stdout_path, "w") as f:
            p = subprocess.run(argv, cwd=cwd, stdout=f, stderr=subprocess.PIPE, text=True)
        return p.returncode, "", p.stderr[-2000:]
    except OSError as e:
        return 127, "", f"{type(e).__name__}: {e}"


def run_tool(tool: str, cred_root: Path, out: Path, bench_bin: Path) -> dict:
    scan, version = commands(tool, out, bench_bin)
    _, ver_out, ver_err = _run(version, cred_root, None)
    raw_path = out / RAW[tool]
    raw_path.unlink(missing_ok=True)
    t0 = time.monotonic()
    code, _, err = _run(scan, cred_root, raw_path if tool in STDOUT_TOOLS else None)
    entry = {"version": (ver_out or ver_err).strip().splitlines()[-1:] or [""], "argv": scan,
             "seconds": round(time.monotonic() - t0, 1), "exit_code": code, "hits": 0, "column_fallbacks": 0,
             "stderr_tail": err[-500:]}
    entry["version"] = entry["version"][0]
    hits_path = out / f"{tool}.jsonl"
    hits_path.unlink(missing_ok=True)
    try:
        raw = raw_path.read_text(errors="replace") if raw_path.exists() else ""
        found, fallbacks = PARSERS[tool](raw, cred_root) if raw.strip() else ([], 0)
    except (ValueError, KeyError, TypeError) as e:
        entry["parse_error"] = f"{type(e).__name__}: {e}"[:300]
        found, fallbacks = [], 0
    H.write_hits(hits_path, found)
    entry.update(hits=len(found), column_fallbacks=fallbacks)
    return entry


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m evals.creddata.run_tools")
    ap.add_argument("--cred-root", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--only", nargs="+", choices=TOOLS)
    ap.add_argument("--bench-bin", type=Path, default=Path("/opt/miniconda3/envs/hermie-bench/bin"))
    a = ap.parse_args(argv)
    cred_root, out = a.cred_root.expanduser().resolve(), a.out.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / "tools_manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    for tool in a.only or TOOLS:
        print(f"running {tool}", flush=True)
        manifest[tool] = run_tool(tool, cred_root, out, a.bench_bin)
        manifest_path.write_text(json.dumps(manifest, indent=1) + "\n")
        e = manifest[tool]
        print(f"  {tool} {e['version']}: exit {e['exit_code']}, {e['hits']} hits, {e['seconds']}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
