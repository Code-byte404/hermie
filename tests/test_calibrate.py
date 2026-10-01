"""Routing calibration: labels derived from what followed each routed task; thresholds swept on the recorded signals."""
import json

from hermie import calibrate
from hermie.config import Settings

SIG = {"privacy": {"sensitive": False}, "task_type": {"choice": "simple", "confidence": 0.9},
       "task_probs": {"simple": 0.9}, "complexity": {"score": 0, "confidence": 0.9}, "complexity_probs": [0.9, 0.1, 0],
       "routellm_win_rate": None, "needs_workspace": True, "needs_workspace_prob": 0.67}


def rec(route="local", *, backend="ollama", nodes=(), sha="a", force="none", escalated=False, delegations=0,
        interrupted=False, fallback=False, sensitive=False, signals=None, ts="2026-09-28T10:00:00"):
    return {"ts": ts, "input_sha256": sha, "route": route, "backend": backend, "force": force, "interrupted": interrupted,
            "fallback": fallback, "sensitive": sensitive, "escalated": escalated, "delegations": delegations,
            "signals": signals or json.loads(json.dumps(SIG)), "nodes": list(nodes)}


def ex(status="done"):
    return {"node": "execute", "status": "ok", "report_status": status}


def rv(passed, rnd=1):
    return {"node": "review", "status": "done", "passed": passed, "round": rnd}


def test_labels():
    assert calibrate.label(rec("local", nodes=[ex(), rv(True)]), []) == ["local", "local_verify"]
    assert calibrate.label(rec("local", nodes=[ex(), rv(False)]), []) == ["local_verify", "plan"]
    assert calibrate.label(rec("local", nodes=[ex("partial")]), []) == ["local_verify", "plan"]
    assert calibrate.label(rec("local_verify", nodes=[ex(), rv(True)]), []) == ["local_verify", "local"]
    assert calibrate.label(rec("local_verify", escalated=True, backend="deepseek-plan+ollama"), []) == ["plan"]
    assert calibrate.label(rec("local_verify", escalated=True, backend="deepseek"), []) == ["cloud"]
    assert calibrate.label(rec("plan", delegations=1, nodes=[ex(), rv(True, 1)]), []) == ["local", "local_verify"]
    assert calibrate.label(rec("plan", delegations=2, nodes=[ex(), rv(True)]), []) == ["plan"]
    assert calibrate.label(rec("cloud", backend="deepseek"), []) == ["cloud"]
    assert calibrate.label(rec("cloud", sha="x"), [rec("local", sha="x", force="local")]) == ["local", "local_verify"]
    assert calibrate.label(rec("local", force="local", nodes=[ex(), rv(False)]), []) == ["local", "local_verify"]


def test_labels_exclude_sensitive_interrupted_fallback():
    assert calibrate.label(rec(interrupted=True), []) is None
    assert calibrate.label(rec(fallback=True), []) is None
    assert calibrate.label(rec(sensitive=True), []) is None
    bad = rec()
    del bad["signals"]["task_type"]
    assert calibrate.label(bad, []) is None


def _write(settings, records):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.trajectory_log_path.write_text("".join(json.dumps(r) + "\n" for r in records))


def test_calibrate_refuses_below_minimum(tmp_path):
    s = Settings(data_dir=tmp_path / "d", env_path=tmp_path / ".env", calibrate_min_tasks=30)
    r = calibrate.build_report(s)
    assert r.tasks == 0 and not r.can_apply and "No trajectories" in r.note
    _write(s, [rec("local", nodes=[ex(), rv(True)])] * 5)
    r = calibrate.build_report(s)
    assert r.labelled == 5 and not r.can_apply and "30" in r.note
    assert calibrate.apply(s, r) == {} and not (tmp_path / ".env").exists()


def test_report_proposes_and_apply_writes_env(tmp_path):
    s = Settings(data_dir=tmp_path / "d", env_path=tmp_path / ".env", calibrate_min_tasks=3, min_confidence=0.6)
    # judged "hard" with confidence 0.8: at MIN_CONFIDENCE 0.6 they go to plan mode, but the user forced them local;
    # at 0.9 the judge is not trusted and they run local_verify, which the label accepts
    records = [rec("local", force="local", sha=str(i), nodes=[ex(), rv(True)]) for i in range(4)]
    for r in records:
        r["signals"]["task_type"] = {"choice": "complex", "confidence": 0.8}
        r["signals"]["complexity"] = {"score": 2, "confidence": 0.8}
    _write(s, records)
    r = calibrate.build_report(s)
    assert r.can_apply and r.traj_acc_current == 0.0 and r.traj_acc_proposed == 1.0
    assert r.proposed["min_confidence"] == 0.9 and len(r.changed) == 4
    md = calibrate.format_report(r)
    assert "MIN_CONFIDENCE" in md and "| Setting |" in md
    written = calibrate.apply(s, r)
    assert written == {"MIN_CONFIDENCE": "0.9"} and "MIN_CONFIDENCE=0.9" in (tmp_path / ".env").read_text()


def test_apply_noop_when_proposal_is_current(tmp_path):
    s = Settings(data_dir=tmp_path / "d", env_path=tmp_path / ".env", calibrate_min_tasks=1)
    _write(s, [rec("local", nodes=[ex(), rv(True)], sha=str(i)) for i in range(3)])
    r = calibrate.build_report(s)
    assert r.proposed == r.current and calibrate.apply(s, r) == {} and "already" in calibrate.format_report(r)


def test_since_filters_old_records(tmp_path):
    s = Settings(data_dir=tmp_path / "d", env_path=tmp_path / ".env")
    _write(s, [rec(ts="2000-01-01T00:00:00"), rec(ts="2999-01-01T00:00:00")])
    assert len(calibrate.load_trajectories(s.trajectory_log_path, since_days=30)) == 1


def test_router_uses_needs_workspace_threshold_setting(monkeypatch):
    monkeypatch.setenv("NEEDS_WORKSPACE_THRESHOLD", "0.5")
    assert Settings().needs_workspace_threshold == 0.5


def test_eval_cases_anchor_the_proposal(tmp_path):
    s = Settings(data_dir=tmp_path / "d", env_path=tmp_path / ".env", calibrate_min_tasks=3, min_confidence=0.6)
    records = [rec("local", force="local", sha=str(i), nodes=[ex(), rv(True)]) for i in range(4)]
    for r in records:
        r["signals"]["task_type"] = {"choice": "complex", "confidence": 0.8}
        r["signals"]["complexity"] = {"score": 2, "confidence": 0.8}
    _write(s, records)
    evals = tmp_path / "signals.jsonl"
    ev = [dict(r, expect=["plan"]) for r in records]  # the curated set says these hard tasks belong to plan mode
    evals.write_text("".join(json.dumps(e) + "\n" for e in ev))
    r = calibrate.build_report(s, eval_signals=evals)
    assert r.proposed == r.current and not r.can_apply
    assert "eval" in r.note.lower()


def test_report_says_when_no_eval_signals(tmp_path):
    s = Settings(data_dir=tmp_path / "d", env_path=tmp_path / ".env", calibrate_min_tasks=1)
    _write(s, [rec("local", nodes=[ex(), rv(True)])])
    md = calibrate.format_report(calibrate.build_report(s, eval_signals=tmp_path / "missing.jsonl"))
    assert "No eval signals" in md


def test_rejected_plan_is_not_labelled():
    from hermie.calibrate import label
    rec = {"route": "plan", "backend": "deepseek-plan+ollama", "signals": {"task_type": "planning"},
           "nodes": [{"node": "design", "status": "rejected"}], "input_sha256": "x"}
    assert label(rec, []) is None
