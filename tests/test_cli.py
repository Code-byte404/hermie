"""CLI helpers that need no models."""
from pathlib import Path

from hermie.cli import read_material


def test_read_material_accepts_file_or_directory(settings, tmp_path):
    f = tmp_path / "in.txt"
    f.write_text("body text")
    d = tmp_path / "proj"
    (d / "a").mkdir(parents=True)
    (d / "a" / "b.py").write_text("print(1)")
    assert read_material(f, settings) == f"[File: {f}]\nbody text"
    tree = read_material(d, settings)
    assert tree.startswith(f"[Directory: {d}]") and "a/b.py" in tree and "print(1)" not in tree


def test_read_material_missing_path_exits(settings, tmp_path):
    import pytest
    with pytest.raises(SystemExit):
        read_material(tmp_path / "nope.txt", settings)


def test_graph_flag_prints_mermaid_without_loading_models(capsys, monkeypatch):
    import hermie.core
    from hermie import cli

    def boom(*a, **k):
        raise AssertionError("--graph must not construct the agent")
    monkeypatch.setattr(hermie.core, "Hermie", boom)
    cli.main(["--graph"])
    out = capsys.readouterr().out
    assert "stateDiagram-v2" in out and "route --> by_route" in out


def test_calibrate_flag_prints_report_and_applies(tmp_path, monkeypatch, capsys):
    import json
    from hermie import cli
    data = tmp_path / "d"
    data.mkdir()
    env = tmp_path / ".env"
    monkeypatch.setenv("HERMIE_DATA_DIR", str(data))
    monkeypatch.setattr("hermie.config.PROJECT_ROOT", tmp_path)
    cli.main(["--calibrate"])
    assert "No trajectories" in capsys.readouterr().out
    rec = {"ts": "2999-01-01T00:00:00", "input_sha256": "a", "route": "local", "backend": "ollama", "force": "local",
           "interrupted": False, "fallback": False, "sensitive": False, "escalated": False, "delegations": 0,
           "signals": {"privacy": {"sensitive": False}, "task_type": {"choice": "complex", "confidence": 0.8},
                       "complexity": {"score": 2, "confidence": 0.8}, "needs_workspace": True,
                       "needs_workspace_prob": 0.67, "routellm_win_rate": None},
           "nodes": [{"node": "review", "passed": True, "round": 1}]}
    (data / "trajectories.jsonl").write_text("".join(json.dumps({**rec, "input_sha256": str(i)}) + "\n" for i in range(3)))
    monkeypatch.setenv("CALIBRATE_MIN_TASKS", "2")
    monkeypatch.setenv("MIN_CONFIDENCE", "0.6")
    cli.main(["--calibrate"])
    assert "hermie --calibrate --apply" in capsys.readouterr().out and not env.exists()
    cli.main(["--calibrate", "--apply"])
    out = capsys.readouterr().out
    assert "MIN_CONFIDENCE" in out and "Wrote" in out and "MIN_CONFIDENCE=0.9" in env.read_text()
