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
