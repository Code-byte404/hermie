"""AGENT.md project document + the current directory as the workspace."""
from pathlib import Path

import pytest

from hermie import project_doc
from hermie.cli import resolve_workspace

from .conftest import FakeJudge, Script, final, text, tool

PHONE = "13812345678"


def test_record_creates_template_and_appends(tmp_path):
    e1 = project_doc.format_entry("Clean up the data", "local only", "Done", ["Read 1 file"], ["out/a.csv"], [])
    p = project_doc.record_progress(tmp_path, e1)
    assert p.name == "AGENT.md" and "## About" in p.read_text() and "`out/a.csv`" in p.read_text()
    project_doc.record_progress(tmp_path, project_doc.format_entry("Second task", "local only", "Failed", [], [], ["Timed out"]))
    t = p.read_text()
    assert t.count("### ") == 2 and t.index("Clean up the data") < t.index("Second task") and "Timed out" in t


def test_record_respects_existing_lowercase_doc_and_caps_entries(tmp_path):
    (tmp_path / "agent.md").write_text("# My project\n\n## Current status\n\nHalfway there\n", encoding="utf-8")
    for i in range(project_doc.MAX_ENTRIES + 5):
        project_doc.record_progress(tmp_path, project_doc.format_entry(f"task{i}", "local only", "Done", [], [], []))
    t = (tmp_path / "agent.md").read_text()
    assert "Halfway there" in t and t.count("### ") == project_doc.MAX_ENTRIES and "task4" not in t and "task5" in t
    # the macOS file system is case-insensitive, so exists() cannot tell; confirm no second file was created
    assert len([x for x in tmp_path.iterdir() if x.name.lower() == "agent.md"]) == 1


def test_load_truncates_middle(tmp_path):
    (tmp_path / "AGENT.md").write_text("h" * 9000 + "t" * 9000, encoding="utf-8")
    t = project_doc.load(tmp_path)
    assert len(t) < project_doc.MAX_DOC_CHARS + 50 and t.startswith("h") and t.endswith("t") and "middle omitted" in t
    assert project_doc.load(tmp_path / "nope") == ""


async def test_executor_sees_doc_and_progress_is_recorded(make_agent, settings):
    settings.workspace.mkdir(parents=True, exist_ok=True)
    (settings.workspace / "AGENT.md").write_text("# Project\n\n## Current status\n\nLast time we got to step two\n",
                                                 encoding="utf-8")
    ex = Script(final=final(steps=["Completed step three"],
                            artifacts=[{"path": "out/x.md", "type": "Markdown", "size_hint": "1 page"}]))
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex)
    await agent.run("Continue with step three")
    assert "Last time we got to step two" in ex.sent_text() and "[Project doc AGENT.md]" in ex.sent_text()
    t = (settings.workspace / "AGENT.md").read_text()
    assert "## Progress log" in t and "Completed step three" in t and "`out/x.md`" in t and "Continue with step three" in t


async def test_doc_with_pii_is_redacted_for_planner(make_agent, settings):
    settings.workspace.mkdir(parents=True, exist_ok=True)
    (settings.workspace / "AGENT.md").write_text(f"# Project\n\nCustomer contact phone {PHONE}\n", encoding="utf-8")
    planner = Script([tool("delegate", step="Write a follow-up note for the customer at <CN_MOBILE_1>"), text("ok")],
                     name="planner")
    ex = Script(final=final())
    agent = make_agent(FakeJudge(task="planning"), executor=ex, planner=planner)
    r = await agent.run("Plan the customer follow-up and write the note")
    assert r.route == "plan"
    assert PHONE not in planner.sent_text() and "<CN_MOBILE_1>" in planner.sent_text()
    assert PHONE in ex.sent_text() and f"customer at {PHONE}" in ex.sent_text()


def test_resolve_workspace_defaults_to_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ws, warnings = resolve_workspace(None)
    assert ws == tmp_path.resolve() and warnings == []
    assert resolve_workspace(tmp_path / "sub")[0] == (tmp_path / "sub").resolve()


def test_resolve_workspace_refuses_home_and_root():
    with pytest.raises(SystemExit):
        resolve_workspace(Path.home())
    with pytest.raises(SystemExit):
        resolve_workspace(Path("/"))


def test_resolve_workspace_warns_for_tcc_dirs():
    _, warnings = resolve_workspace(Path.home() / "Documents" / "proj")
    assert warnings and "Documents" in warnings[0]


async def test_interrupted_task_still_records_progress(make_agent, settings):
    def boom(m, info):
        raise RuntimeError("model died")
    agent = make_agent(FakeJudge(task="repetitive"), executor=Script([boom]))
    with pytest.raises(RuntimeError):
        await agent.run("Do something")
    t = (settings.workspace / "AGENT.md").read_text()
    assert "Interrupted" in t and "Do something" in t   # status label "Interrupted" is written by core._record_progress
