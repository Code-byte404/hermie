"""Connectors wired into the executor: tools, instructions, data room, plan-mode scope."""
import os
import shutil
import time
import uuid
from pathlib import Path

from hermie.connectors.registry import build_connectors, prune_rooms, ready_connectors

from .conftest import FakeConnector, FakeJudge, Script, final, tool


async def test_executor_calls_connector_and_reads_room(make_agent, settings):
    conn = FakeConnector()
    ex = Script([tool("fake_lookup", query="revenue last week")],
                final=final(answer=f"Revenue was {FakeConnector.FIGURE}"))
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex, connectors=[conn])
    r = await agent.run("What was revenue last week?")
    assert conn.calls == [{"query": "revenue last week"}]
    assert FakeConnector.FIGURE in ex.sent_text() and "Full output saved" in ex.sent_text()
    rooms = list(settings.connector_rooms_dir.iterdir())
    assert len(rooms) == 1 and (rooms[0] / "001-fake.json").exists()


async def test_room_is_readable_but_not_writable_from_the_sandbox(make_agent, settings):
    # pytest's tmp_path is inside the sandbox-writable per-user temp dir, so the data dir must live under $HOME here
    data_dir = Path.home() / f".hermie-test-room-{uuid.uuid4().hex[:8]}"
    try:
        conn = FakeConnector()
        ex = Script([tool("fake_lookup", query="q")], final=final())
        agent = make_agent(FakeJudge(task="repetitive"), executor=ex, connectors=[conn], data_dir=data_dir)
        await agent.run("revenue?")
        room = next(settings.connector_rooms_dir.iterdir())
        # The grant ended with the task; re-grant to probe the profile like a running task would
        with agent.session.sandbox.grant_read([room]):
            r = await agent.session.sandbox.run_shell(f"cat {room}/001-fake.json")
            assert r.exit_code == 0 and FakeConnector.FIGURE in r.stdout
            r = await agent.session.sandbox.run_shell(f"echo x > {room}/new.txt")
            assert r.exit_code != 0
        assert not (room / "new.txt").exists()
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


async def test_cheatsheet_only_in_business_tasks(make_agent):
    ex = Script(final=final())
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex, connectors=[FakeConnector()])
    await agent.run("Rename files to lowercase")
    first = ex.infos[0]
    assert "fake_lookup" in [t.name for t in first.function_tools]
    assert "FAKE_CHEATSHEET" not in str(ex.seen[0])
    ex2 = Script(final=final())
    agent2 = make_agent(FakeJudge(task="repetitive"), executor=ex2, connectors=[FakeConnector()])
    await agent2.run("downloads last week?", business=True)
    assert "FAKE_CHEATSHEET" in str(ex2.seen[0])


async def test_plan_mode_executor_has_no_connector_tools(make_agent):
    planner = Script([tool("delegate", step="Write notes.md"), ], name="planner")
    ex = Script(final=final())
    agent = make_agent(FakeJudge(task="planning"), executor=ex, planner=planner, connectors=[FakeConnector()])
    r = await agent.run("Plan and build a small notes tool")
    assert r.route == "plan"
    assert ex.infos and all("fake_lookup" not in [t.name for t in i.function_tools] for i in ex.infos)


def test_unready_connector_becomes_a_startup_note(make_agent):
    agent = make_agent(connectors=[FakeConnector(state="not_authenticated")])
    assert agent.connectors == {}
    assert any("Connector fake unavailable (not_authenticated)" in n for n in agent.startup_notes)


def test_build_connectors_auto_and_explicit(settings, monkeypatch):
    import hermie.connectors.registry as reg
    monkeypatch.setattr(reg.shutil, "which", lambda name: None)
    settings.connectors, settings.asc_path = None, ""
    assert build_connectors(settings) == ([], [])                       # auto, asc absent: silent
    settings.connectors = ["asc"]
    conns, notes = build_connectors(settings)
    assert conns == [] and "asc CLI was not found" in notes[0]
    settings.connectors = ["nope"]
    assert "Unknown connector 'nope'" in build_connectors(settings)[1][0]


def test_duplicate_tool_names_rejected():
    import pytest
    with pytest.raises(ValueError):
        ready_connectors([FakeConnector(), FakeConnector()])


def test_prune_rooms(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir(), new.mkdir()
    past = time.time() - 10 * 86400
    os.utime(old, (past, past))
    prune_rooms(tmp_path, 7)
    assert not old.exists() and new.exists()
