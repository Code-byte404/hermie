"""Score an end-to-end run directory (see run.py): leaks, the bypass cross-check, answer quality and causes.

    python -m evals.e2e.score --out DIR [--judge claude|none] [--peers auto|none] [--bench-bin DIR]

Writes DIR/summary.md, DIR/leaks.md, DIR/quality.md, DIR/reruns.txt (tasks whose arms disagree and have only one
repeat: run them again with `run --tasks ... --rep 2,3`) and DIR/peers.md.

A canary is `leaked` when a valid Hermie-arm run sent it, `protected` when only the direct arm sent it, and `not
exercised` when no run sent it (it says nothing either way). A Hermie-arm run is `invalid` when a recorded body
has no identical body in Hermie's outbound/ (traffic that did not pass through Hermie): its leaks are listed but
do not decide a canary's status. Gate 1: no claimed canary leaked (INCONCLUSIVE while any Hermie run is invalid). Gate 2: no task where the direct arm passes and
the Hermie arm fails for a reason other than noise.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path

from evals.e2e import grade as G
from evals.e2e.leaks import find_leaks
from evals.e2e.tasks import TASKS
from evals.e2e.workspace import KINDS, Canary, load_canaries

CAUSE_ORDER = ("blocked", "withheld", "unrestored", "swapped", "damage")
PEER_KINDS = ("api_key", "password", "url_credentials")


def _runs(out: Path):
    for status in sorted((out / "runs").glob("*/*/rep*/status")):
        rd = status.parent
        yield rd.parent.parent.name, rd.parent.name, int(rd.name[3:]), rd


def _canon(raw: bytes):
    try:
        return json.dumps(json.loads(raw), sort_keys=True, ensure_ascii=False)
    except ValueError:
        return raw.decode("utf-8", errors="replace")


def _bodies(rd: Path) -> list[tuple[int, bytes]]:
    return [(int(p.stem), p.read_bytes()) for p in sorted((rd / "recorded").glob("[0-9][0-9][0-9].json"))]


def _bypassed(rd: Path, bodies: list[tuple[int, bytes]]) -> bool:
    sent = {_canon(p.read_bytes()) for p in (rd / "hermie_data" / "outbound").glob("*.json")}
    return any(raw.strip() and _canon(raw) not in sent for _, raw in bodies)


def _receipt(rd: Path) -> list[dict]:
    p = rd / "hermie_data" / "receipt.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.is_file() else []


def claude_judge(prompt: str) -> str:
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDECODE", "ANTHROPIC_BASE_URL") and not k.startswith("CLAUDE_CODE_")}
    with tempfile.TemporaryDirectory() as d:
        r = subprocess.run(["claude", "-p", prompt, "--output-format", "text"], cwd=d, env=env, capture_output=True,
                           text=True, timeout=300)
    return r.stdout


def _verdict(out: Path, arm: str, task, rep: int, key: str, answer: str, judge) -> tuple[bool, str] | None:
    if judge is None:
        return None
    cache = out / "judge" / arm / task.id / f"rep{rep}.txt"
    if not cache.is_file():
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(judge(G.judge_prompt(task, key, answer)))
    return G.parse_verdict(cache.read_text())


def _cause(task_id: str, reps: dict, quality: dict, receipts: dict) -> str:
    h = [(r, quality[("hermie", task_id, r)]) for r in reps.get("hermie", [])]
    d = [quality[("direct", task_id, r)] for r in reps.get("direct", [])]
    if len(d) > 1 and not all(q["passed"] for q in d):   # the direct arm itself is unsteady
        return "noise"
    rep1, first = next((r, q) for r, q in h if not q["passed"])
    if first["status"] == "blocked":
        return "blocked"
    if any(line.get("withheld_items") for line in receipts.get((task_id, rep1), [])):
        return "withheld"
    if first["checks"]["placeholders_left"]:
        return "unrestored"
    if first["checks"]["wrong"]:
        return "swapped"
    return "damage"


def _majority(qs: list[dict]) -> bool:
    return sum(q["passed"] for q in qs) * 2 > len(qs)


def score(out: Path, judge=None, peers: bool = False, bench_bin: Path | None = None) -> dict:
    out = Path(out)
    canaries = load_canaries(out / "workspace" / "canaries.json")
    by_id = {c.id: c for c in canaries}
    tasks = {t.id: t for t in TASKS}
    leaks, invalid, quality, receipts = [], [], {}, {}
    sent = {"hermie": set(), "direct": set()}
    reps: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for arm, tid, rep, rd in _runs(out):
        bodies = _bodies(rd)
        bad = arm == "hermie" and (_bypassed(rd, bodies) or not bodies)
        if bad:
            invalid.append((arm, tid, rep))
        for n, raw in bodies:
            for lk in find_leaks(raw, canaries):
                if arm == "hermie":
                    leaks.append({"canary_id": lk.canary_id, "task": tid, "rep": rep, "request": n, "path": lk.path,
                                  "form": lk.form, "invalid": bad})
                if not bad:
                    sent[arm].add(lk.canary_id)
        if arm == "hermie":
            receipts[(tid, rep)] = _receipt(rd)
        task = tasks.get(tid)
        if task is None:
            continue
        status = (rd / "status").read_text().strip()
        answer = (rd / "answer.txt").read_text() if (rd / "answer.txt").is_file() else ""
        checks = G.check(task, answer, by_id, rd / "workdir") if status == "ok" else {
            "passed": False, "missing": list(task.must_mention), "wrong": [], "placeholders_left": [],
            "tests_passed": None}
        verdict = _verdict(out, arm, task, rep, G.render(task.rubric, by_id), answer, judge) if status == "ok" else None
        quality[(arm, tid, rep)] = {"status": status, "checks": checks, "verdict": verdict,
                                    "passed": status == "ok" and G.passed(checks, verdict), "invalid": bad}
        reps[tid][arm].append(rep)

    status_of = {c.id: "leaked" if c.id in sent["hermie"] else "protected" if c.id in sent["direct"]
                 else "not exercised" for c in canaries}
    by_kind = {k: {"claimed": claimed, "canaries": 0, "exercised": 0, "leaked": 0} for k, claimed in KINDS.items()}
    for c in canaries:
        row = by_kind[c.kind]
        row["canaries"] += 1
        row["exercised"] += status_of[c.id] != "not exercised"
        row["leaked"] += status_of[c.id] == "leaked"

    causes, reruns, arm_pass = {}, [], {}
    for tid, by_arm in sorted(reps.items()):
        for arm in by_arm:
            by_arm[arm].sort()
        if not (by_arm.get("hermie") and by_arm.get("direct")):
            continue
        h = [quality[("hermie", tid, r)] for r in by_arm["hermie"]]
        d = [quality[("direct", tid, r)] for r in by_arm["direct"]]
        arm_pass[tid] = (_majority(h), _majority(d))
        if len(h) == 1 and h[0]["passed"] != d[0]["passed"]:
            reruns.append(tid)
        if _majority(d) and not _majority(h):
            causes[tid] = _cause(tid, by_arm, quality, receipts)
    report = {"canaries": status_of, "by_kind": by_kind,
              "claimed_leaked": sorted(c.id for c in canaries if c.claimed and status_of[c.id] == "leaked"),
              "leaks": leaks, "invalid": invalid, "quality": quality, "causes": causes, "reruns": reruns,
              "arm_pass": arm_pass, "peers": _peers(out, canaries, bench_bin) if peers else None}
    return report


def _peers(out: Path, canaries: list[Canary], bench_bin: Path | None) -> dict[str, dict[str, bool]] | None:
    ws = out / "workspace"
    flagged: dict[str, set[tuple[str, int]]] = {"gitleaks": set(), "detect_secrets": set()}
    with tempfile.TemporaryDirectory() as d:
        rep = Path(d) / "gl.json"
        try:
            subprocess.run(["gitleaks", "dir", ".", "--report-format", "json", "--report-path", str(rep),
                            "--no-banner", "--exit-code", "0"], cwd=ws, capture_output=True, timeout=300)
            for f in json.loads(rep.read_text() or "[]"):
                flagged["gitleaks"].add((f["File"].removeprefix("./"), f["StartLine"]))
        except (OSError, ValueError, subprocess.SubprocessError):
            flagged.pop("gitleaks")
    ds = str(Path(bench_bin or "/opt/miniconda3/envs/hermie-bench/bin") / "detect-secrets")
    try:
        r = subprocess.run([ds, "scan", "--all-files", "."], cwd=ws, capture_output=True, text=True, timeout=300)
        for path, items in json.loads(r.stdout).get("results", {}).items():
            for it in items:
                flagged["detect_secrets"].add((path.removeprefix("./"), it["line_number"]))
    except (OSError, ValueError, subprocess.SubprocessError):
        flagged.pop("detect_secrets")
    if not flagged:
        return None
    out_: dict[str, dict[str, bool]] = {}
    for c in canaries:
        if c.kind not in PEER_KINDS:
            continue
        lines = (ws / c.file).read_text(encoding="utf-8").splitlines()
        where = {(c.file, i) for i, line in enumerate(lines, 1) if c.value in line}
        out_[c.id] = {tool: bool(where & hits) for tool, hits in flagged.items()}
    return out_


# ---------------------------------------------------------------- report

def _gate(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def write_reports(out: Path, r: dict) -> None:
    out = Path(out)
    manifest = json.loads((out / "manifest.json").read_text()) if (out / "manifest.json").is_file() else {}
    by_id = {c.id: c for c in load_canaries(out / "workspace" / "canaries.json")}
    q = r["quality"]
    lines = ["# End-to-end canary run", "",
             f"Hermie {manifest.get('hermie_commit', '?')[:7]}, Claude Code {manifest.get('claude_version', '?')}, "
             f"model {manifest.get('model', '?')}, seed {manifest.get('seed', '?')}, "
             f"sessions {manifest.get('sessions', '?')}, config {manifest.get('hermie_config', {})}.", ""]
    statuses = defaultdict(lambda: defaultdict(int))
    for (arm, _, _), v in q.items():
        statuses[arm][v["status"]] += 1
    lines += ["## Runs", ""] + [f"- {arm}: " + ", ".join(f"{k} {n}" for k, n in sorted(s.items()))
                                for arm, s in sorted(statuses.items())]
    lines += [f"- invalid (traffic not through Hermie): {len(r['invalid'])}"
              + (" " + ", ".join(f"{t} rep{n}" for _, t, n in r["invalid"]) if r["invalid"] else ""), ""]
    in_invalid = sorted({l["canary_id"] for l in r["leaks"] if l["invalid"] and by_id[l["canary_id"]].claimed})
    gate1 = "FAIL" if r["claimed_leaked"] else "INCONCLUSIVE" if r["invalid"] else "PASS"
    lines += [f"## Gate 1 (claimed kinds leak nothing): {gate1}", "",
              f"Claimed canaries leaked: {len(r['claimed_leaked'])}"
              + (" (" + ", ".join(r["claimed_leaked"]) + ")" if r["claimed_leaked"] else ""), ""]
    if r["invalid"]:
        lines += [f"Invalid Hermie runs exist; claimed canaries seen in them: "
                  f"{', '.join(in_invalid) if in_invalid else 'none'}.", ""]
    lines += [
              "| kind | claimed | canaries | exercised | leaked |", "|---|---|---|---|---|"]
    for kind, row in sorted(r["by_kind"].items(), key=lambda kv: (not kv[1]["claimed"], -kv[1]["leaked"], kv[0])):
        lines.append(f"| {kind} | {'yes' if row['claimed'] else 'no'} | {row['canaries']} | {row['exercised']} | "
                     f"{row['leaked']} |")
    hermie_caused = {t: c for t, c in r["causes"].items() if c != "noise"}
    gate2 = not hermie_caused
    passes = {arm: sum(1 for (a, _, rep), v in q.items() if a == arm and rep == 1 and v["passed"]) for arm in
              ("hermie", "direct")}
    totals = {arm: sum(1 for (a, _, rep) in q if a == arm and rep == 1) for arm in ("hermie", "direct")}
    lines += ["", f"## Gate 2 (no Hermie-caused wrong answer): {_gate(gate2)}", "",
              f"Pass rate, first repeat: hermie {passes['hermie']}/{totals['hermie']}, "
              f"direct {passes['direct']}/{totals['direct']}.", ""]
    lines += [f"- {t}: {c}" for t, c in sorted(r["causes"].items())] or ["- no task where direct passes and Hermie "
                                                                          "fails"]
    if r["reruns"]:
        lines += ["", f"Arms disagree on {len(r['reruns'])} task(s) with one repeat: see reruns.txt."]
    if r["peers"]:
        tools = sorted({t for v in r["peers"].values() for t in v})
        lines += ["", "## Key canaries flagged by peer scanners", "",
                  "| canary | " + " | ".join(tools) + " | hermie |", "|---|" + "---|" * (len(tools) + 1)]
        for cid, v in sorted(r["peers"].items()):
            lines.append(f"| {cid} | " + " | ".join("yes" if v.get(t) else "no" for t in tools)
                         + f" | {r['canaries'][cid]} |")
    (out / "summary.md").write_text("\n".join(lines) + "\n")

    lk = ["# Hermie-arm leaks", "", "| canary | kind | claimed | task | rep | request | path | form | invalid run |",
          "|---|---|---|---|---|---|---|---|---|"]
    for l in sorted(r["leaks"], key=lambda l: (not by_id[l["canary_id"]].claimed, l["canary_id"], l["task"])):
        c = by_id[l["canary_id"]]
        lk.append(f"| {c.id} | {c.kind} | {'yes' if c.claimed else 'no'} | {l['task']} | {l['rep']} | "
                  f"{l['request']:03d} | `{l['path']}` | {l['form']} | {'yes' if l['invalid'] else ''} |")
    lk += ["", "## Canary status", ""] + [f"- {cid}: {s}" for cid, s in sorted(r["canaries"].items())]
    (out / "leaks.md").write_text("\n".join(lk) + "\n")

    ql = ["# Answer quality", "", "| task | arm | rep | status | passed | missing | wrong | placeholders | tests | "
          "judge |", "|---|---|---|---|---|---|---|---|---|---|"]
    for (arm, tid, rep), v in sorted(q.items(), key=lambda kv: (kv[0][1], kv[0][2], kv[0][0])):
        ch, vd = v["checks"], v["verdict"]
        ql.append(f"| {tid} | {arm} | {rep} | {v['status']} | {'yes' if v['passed'] else 'no'} | "
                  f"{', '.join(ch['missing'])} | {', '.join(ch['wrong'])} | {' '.join(ch['placeholders_left'])} | "
                  f"{'' if ch['tests_passed'] is None else ch['tests_passed']} | "
                  f"{'' if vd is None else ('PASS' if vd[0] else 'FAIL') + ': ' + vd[1].replace('|', '/')} |")
    (out / "quality.md").write_text("\n".join(ql) + "\n")
    (out / "reruns.txt").write_text(",".join(r["reruns"]) + ("\n" if r["reruns"] else ""))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m evals.e2e.score")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--judge", choices=("claude", "none"), default="claude")
    ap.add_argument("--peers", choices=("auto", "none"), default="auto")
    ap.add_argument("--bench-bin", type=Path, default=None)
    a = ap.parse_args(argv)
    out = a.out.expanduser().resolve()
    r = score(out, judge=claude_judge if a.judge == "claude" else None, peers=a.peers == "auto",
              bench_bin=a.bench_bin)
    write_reports(out, r)
    print(f"wrote {out / 'summary.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
