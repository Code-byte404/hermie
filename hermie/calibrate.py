"""Routing calibration from Hermie's own trajectories (data_dir/trajectories.jsonl).

Each finished task is labelled with the routes that would have been right, judged from what followed: local work that
never passed review should have gone further, a local_verify that escalated should have gone where it escalated to,
a one-step plan that passed its first review could have stayed local, a cloud answer the user re-ran locally should
have been local, and a forced route is what the user wanted. Interrupted tasks, tasks that fell back after a cloud
failure and privacy-sensitive tasks are not labelled (the sensitive branch of policy.decide is not tuned here, and
privacy thresholds are never tuned from usage).

The routing thresholds are then swept over the recorded signals, replaying the pure policy.decide. Nothing leaves the
machine; `.env` changes only through apply(), which the CLI calls for --apply.
"""
from __future__ import annotations

import itertools
import json
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import Settings, update_env
from .judge import ChoiceAnswer, ScoreAnswer
from .policy import Signals, decide

GRID = {"min_confidence": [0.5, 0.6, 0.67, 0.75, 0.9],
        "routellm_threshold": [0.3, 0.4, 0.5, 0.6, 0.7],
        "needs_workspace_threshold": [0.1, 0.3, 0.5, 0.67]}
ENV_KEYS = {"min_confidence": "MIN_CONFIDENCE", "routellm_threshold": "ROUTELLM_THRESHOLD",
            "needs_workspace_threshold": "NEEDS_WORKSPACE_THRESHOLD"}
LOCALISH = ("local", "local_verify")


def signals_from_record(rec: dict, needs_ws_threshold: float) -> Signals:
    sg = rec["signals"]
    task = ChoiceAnswer(sg["task_type"]["choice"], sg.get("task_probs") or {}, sg["task_type"]["confidence"])
    cx = ScoreAnswer(sg["complexity"]["score"], sg.get("complexity_probs") or [], sg["complexity"]["confidence"])
    p = sg.get("needs_workspace_prob")
    needs_ws = (p > needs_ws_threshold) if p is not None else sg["needs_workspace"]
    return Signals(sg["privacy"]["sensitive"], task, cx, sg.get("routellm_win_rate"), needs_ws, p)


def _settings_for(cfg: dict, base: Optional[Settings]) -> Settings:
    s = Settings() if base is None else Settings(**{k: getattr(base, k) for k in base.__dataclass_fields__})
    for k, v in cfg.items():
        setattr(s, k, v)
    return s


def _hits(records: list[dict], cfg: dict, base: Optional[Settings]) -> tuple[float, float]:
    """(hit rate, share routed local or local_verify) of cfg on the labelled records."""
    if not records:
        return 0.0, 0.0
    s = _settings_for(cfg, base)
    hits = local = 0
    for rec in records:
        route = decide(signals_from_record(rec, cfg["needs_workspace_threshold"]), s).route.value
        hits += route in rec["expect"]
        local += route in LOCALISH
    return hits / len(records), local / len(records)


def sweep(records: list[dict], grid: dict[str, list[float]], base: Optional[Settings] = None) -> list[tuple[float, dict]]:
    """[(hit rate, config)] over every grid combination, best first. Pure computation."""
    keys = list(grid)
    results = [(_hits(records, dict(zip(keys, values)), base)[0], dict(zip(keys, values)))
               for values in itertools.product(*(grid[k] for k in keys))]
    results.sort(key=lambda x: -x[0])
    return results


def _ok(rec: dict) -> bool:
    reviews = [n for n in rec["nodes"] if n.get("node") == "review" and "passed" in n]
    execs = [n for n in rec["nodes"] if n.get("node") == "execute"]
    return (not reviews or reviews[-1]["passed"]) and (not execs or execs[-1].get("report_status") == "done")


def label(rec: dict, later: list[dict]) -> Optional[list[str]]:
    """The routes that would have been right for this finished task, or None when it cannot be judged."""
    if rec.get("interrupted") or rec.get("fallback") or rec.get("sensitive"):
        return None
    if "task_type" not in rec.get("signals", {}):
        return None
    route, force = rec["route"], rec.get("force", "none")
    if force == "local":
        return list(LOCALISH)
    if force == "cloud":
        return [route]
    if route == "local":
        if _ok(rec):
            return list(LOCALISH)
        return ["local_verify", "plan" if rec["signals"].get("needs_workspace", True) else "cloud"]
    if route == "local_verify":
        if rec.get("escalated"):
            return ["plan"] if rec["backend"].endswith("-plan+ollama") else ["cloud"]
        return ["local_verify", "local"]
    if route == "plan":
        reviews = [n for n in rec["nodes"] if n.get("node") == "review" and "passed" in n]
        if rec.get("delegations") == 1 and reviews and reviews[0]["passed"] and reviews[0].get("round") == 1 and _ok(rec):
            return list(LOCALISH)
        return ["plan"]
    if route == "cloud":
        if any(l.get("input_sha256") == rec["input_sha256"] and l.get("force") == "local" for l in later):
            return list(LOCALISH)
        return ["cloud"]
    return None


def labelled(records: list[dict]) -> list[dict]:
    out = []
    for i, rec in enumerate(records):
        exp = label(rec, records[i + 1:])
        if exp is not None:
            out.append({**rec, "expect": exp})
    return out


def load_trajectories(path: Path, since_days: Optional[float] = None) -> list[dict]:
    if not path.exists():
        return []
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - since_days * 86400)) if since_days else None
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if cutoff is None or rec.get("ts", "") >= cutoff:
            out.append(rec)
    return out


@dataclass
class Report:
    tasks: int = 0
    labelled: int = 0
    labels: Counter = field(default_factory=Counter)
    current: dict = field(default_factory=dict)
    proposed: dict = field(default_factory=dict)
    traj_acc_current: float = 0.0
    traj_acc_proposed: float = 0.0
    eval_acc_current: Optional[float] = None
    eval_acc_proposed: Optional[float] = None
    changed: list = field(default_factory=list)   # (input sha prefix, route now, route with proposal)
    can_apply: bool = False
    note: str = ""


def build_report(s: Settings, since_days: Optional[float] = None, eval_signals: Optional[Path] = None) -> Report:
    r = Report()
    r.current = {k: getattr(s, k) for k in GRID}
    r.proposed = dict(r.current)
    records = load_trajectories(s.trajectory_log_path, since_days)
    r.tasks = len(records)
    if not records:
        r.note = "No trajectories yet: run some tasks first (they are recorded in trajectories.jsonl)."
        return r
    lab = labelled(records)
    r.labelled = len(lab)
    r.labels = Counter("/".join(x["expect"]) for x in lab)
    evals = []
    if eval_signals is not None and eval_signals.exists():
        evals = [e for e in load_trajectories(eval_signals) if "signals" in e and "task_type" in e["signals"]]
    if not lab:
        r.note = "No task could be labelled (interrupted, fallback and privacy-sensitive tasks are skipped)."
        return r
    r.traj_acc_current = _hits(lab, r.current, s)[0]
    r.eval_acc_current = _hits(evals, r.current, s)[0] if evals else None
    keys = list(GRID)

    def rank(cfg):
        acc, local = _hits(lab, cfg, s)
        ev = _hits(evals, cfg, s)[0] if evals else 0.0
        distance = sum(abs(cfg[k] - r.current[k]) for k in keys)
        return (acc, ev, local, -distance)
    candidates = [dict(zip(keys, v)) for v in itertools.product(*(GRID[k] for k in keys))] + [dict(r.current)]
    best = max(candidates, key=rank)
    if rank(best) > rank(r.current):
        r.proposed = best
    r.traj_acc_proposed = _hits(lab, r.proposed, s)[0]
    r.eval_acc_proposed = _hits(evals, r.proposed, s)[0] if evals else None
    for rec in lab:
        now = decide(signals_from_record(rec, r.current["needs_workspace_threshold"]), _settings_for(r.current, s)).route.value
        new = decide(signals_from_record(rec, r.proposed["needs_workspace_threshold"]), _settings_for(r.proposed, s)).route.value
        if now != new:
            r.changed.append((rec["input_sha256"][:8], now, new))
    r.can_apply = r.labelled >= s.calibrate_min_tasks and r.proposed != r.current
    if r.labelled < s.calibrate_min_tasks:
        r.note = f"Only {r.labelled} labelled tasks; --apply needs at least {s.calibrate_min_tasks} (CALIBRATE_MIN_TASKS)."
    elif r.proposed == r.current:
        r.note = "The current thresholds are already the best on this record."
    return r


def format_report(r: Report) -> str:
    lines = [f"**Routing calibration** · {r.tasks} tasks recorded, {r.labelled} labelled"]
    if r.labels:
        lines.append("\nLabels: " + ", ".join(f"{k} ×{v}" for k, v in r.labels.most_common()))
    if r.current:
        lines.append("\n| Setting | Current | Proposed |\n|---|---|---|")
        lines += [f"| {ENV_KEYS[k]} | {r.current[k]} | {r.proposed[k]} |" for k in GRID]
        lines.append(f"\nHit rate on your tasks: {r.traj_acc_current:.2f} now, {r.traj_acc_proposed:.2f} proposed")
        if r.eval_acc_current is not None:
            lines.append(f"Hit rate on the eval cases: {r.eval_acc_current:.2f} now, {r.eval_acc_proposed:.2f} proposed")
    if r.changed:
        lines.append(f"\nTasks whose route would change ({len(r.changed)}):")
        lines += [f"- {sha}: {a} → {b}" for sha, a, b in r.changed[:20]]
    if r.note:
        lines.append("\n" + r.note)
    if r.can_apply:
        lines.append("\nRun `hermie --calibrate --apply` to write the proposal to .env.")
    return "\n".join(lines)


def apply(s: Settings, r: Report) -> dict[str, str]:
    """Write the proposed thresholds to .env (only what changed); nothing when the report does not allow it."""
    if not r.can_apply:
        return {}
    values = {ENV_KEYS[k]: str(r.proposed[k]) for k in GRID if r.proposed[k] != r.current[k]}
    if values:
        update_env(s.env_path, values)
        for k in GRID:
            setattr(s, k, r.proposed[k])
    return values
