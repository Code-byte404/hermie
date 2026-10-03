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


class _Broken(FakeConnector):
    name = "broken"

    def __init__(self, where):
        super().__init__()
        self.where = where

    def status(self):
        if self.where == "status":
            raise RuntimeError("status exploded")
        return super().status()

    def tools(self):
        if self.where == "tools":
            raise RuntimeError("tools exploded")
        return super().tools()


class _Other(FakeConnector):
    name = "other"


def test_duplicate_tool_names_drop_the_later_connector():
    first, second = FakeConnector(), _Other()
    ready, notes = ready_connectors([first, second])
    assert ready == [first]
    assert any("Connector other unavailable" in n and "fake_lookup" in n for n in notes)


def test_status_raising_becomes_a_note():
    ready, notes = ready_connectors([_Broken("status")])
    assert ready == [] and "Connector broken unavailable (error): RuntimeError: status exploded" in notes[0]


def test_broken_connectors_never_stop_startup(make_agent):
    good = FakeConnector()
    agent = make_agent(connectors=[_Broken("tools"), good, _Other()])
    assert agent.connectors == {"fake": good}
    assert any("Connector broken unavailable (error): RuntimeError: tools exploded" in n for n in agent.startup_notes)
    assert any("Connector other unavailable" in n for n in agent.startup_notes)
    assert "fake_lookup" in agent.executor._function_toolset.tools


def test_writable_data_room_is_a_startup_note(make_agent):
    # the fixture's data dir is under tmp_path, inside the sandbox-writable per-user temp dir
    agent = make_agent(connectors=[FakeConnector()])
    assert any("would not be read-only" in n and "HERMIE_DATA_DIR" in n for n in agent.startup_notes)


def test_data_room_outside_writable_dirs_has_no_note(make_agent):
    data_dir = Path.home() / f".hermie-test-room-{uuid.uuid4().hex[:8]}"
    try:
        agent = make_agent(connectors=[FakeConnector()], data_dir=data_dir)
        assert not any("would not be read-only" in n for n in agent.startup_notes)
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


def test_prune_rooms_ignores_os_errors(tmp_path, monkeypatch):
    def boom(self):
        raise PermissionError("denied")
    monkeypatch.setattr(Path, "iterdir", boom)
    prune_rooms(tmp_path, 7)   # logged, not raised


async def test_room_mkdir_failure_still_records_the_task(make_agent, settings):
    import json
    import pytest
    agent = make_agent(FakeJudge(task="repetitive"), connectors=[FakeConnector()])
    settings.connector_rooms_dir.parent.mkdir(parents=True, exist_ok=True)
    settings.connector_rooms_dir.write_text("not a directory")
    with pytest.raises(OSError):
        await agent.run("revenue?")
    lines = settings.audit_log_path.read_text().strip().splitlines()
    assert lines and json.loads(lines[-1])["route"] == "cancelled"
    assert settings.trajectory_log_path.read_text().strip()


async def test_cheatsheet_arrives_after_a_connector_call_in_a_plain_task(make_agent):
    ex = Script([tool("fake_lookup", query="downloads")], final=final())
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex, connectors=[FakeConnector()])
    await agent.run("How are things going?")
    assert "FAKE_CHEATSHEET" not in str(ex.seen[0])
    assert "FAKE_CHEATSHEET" in str(ex.seen[1])


async def test_plan_mode_executor_prompt_never_mentions_connectors(make_agent):
    planner = Script([tool("delegate", step="Write notes.md")], name="planner")
    ex = Script(final=final())
    agent = make_agent(FakeJudge(task="planning"), executor=ex, planner=planner, connectors=[FakeConnector()])
    r = await agent.run("Plan and build a small notes tool")
    assert r.route == "plan" and ex.seen
    assert all("Business-data tools" not in str(m) and "FAKE_CHEATSHEET" not in str(m) for m in ex.seen)


async def test_plain_task_executor_prompt_describes_connector_tools(make_agent):
    ex = Script(final=final())
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex, connectors=[FakeConnector()])
    await agent.run("Rename files to lowercase")
    assert "Business-data tools" in str(ex.seen[0])


def test_prune_rooms(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir(), new.mkdir()
    past = time.time() - 10 * 86400
    os.utime(old, (past, past))
    prune_rooms(tmp_path, 7)
    assert not old.exists() and new.exists()


async def test_run_command_asc_is_refused_with_a_hint(make_agent):
    from hermie.events import CommandFinished
    ex = Script([tool("run_command", command="asc --version"),
                 tool("run_command", command="cd sub && /opt/homebrew/bin/asc reviews list")], final=final())
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex, connectors=[FakeConnector()])
    await agent.run("list my reviews")
    from .test_web import tool_returns
    assert sum("use the `asc` tool instead" in r for r in tool_returns(ex)) == 2
    assert not [e for e in agent.events if isinstance(e, CommandFinished) and "asc" in e.command]


def test_asc_command_detection():
    from hermie.capabilities import runs_asc
    for cmd in ("asc --version", "cd x && asc apps list", "/opt/homebrew/bin/asc reviews list", "FOO=1 asc x",
                "env asc x", "echo a | asc x", "(asc x)", "true; sudo asc x", "$(asc x)", "`asc x`"):
        assert runs_asc(cmd), cmd
    for cmd in ("echo asc", "ascii x", "cat asc.txt", "ls ./asc-data", "grep asc file"):
        assert not runs_asc(cmd), cmd


def test_build_connectors_runs_asc_in_the_connector_dir(settings, tmp_path):
    fake = tmp_path / "asc"
    fake.write_text("")
    settings.connectors, settings.asc_path = ["asc"], str(fake)
    conns, _ = build_connectors(settings)
    assert conns[0].cwd == settings.connector_dir


async def test_data_room_readable_only_once_the_task_turns_business(make_agent, settings):
    """The room is created at task start but granted to the sandbox only when the task turns business (here: the
    connector call), and taken away at task end."""
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    data_dir = Path.home() / f".hermie-test-room-{uuid.uuid4().hex[:8]}"
    try:
        def room():
            return next(settings.connector_rooms_dir.iterdir())

        def probe(cmd):
            return lambda m, info: ModelResponse(parts=[ToolCallPart("run_command", {"command": cmd(room())})])
        ex = Script([probe(lambda r: f"ls {r}"), tool("fake_lookup", query="q"),
                     probe(lambda r: f"cat {r}/001-fake.json")], final=final())
        agent = make_agent(FakeJudge(task="repetitive"), executor=ex, connectors=[FakeConnector()],
                           data_dir=data_dir)
        await agent.run("how are we doing?")
        from .test_web import tool_returns
        before, _, after = tool_returns(ex)
        assert "exit_code=0" not in before and "not permitted" in before
        assert "exit_code=0" in after and FakeConnector.FIGURE in after
        r = await agent.session.sandbox.run_shell(f"cat {room()}/001-fake.json")
        assert r.exit_code != 0 and FakeConnector.FIGURE not in r.stdout
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


async def test_prefixed_business_task_room_granted_from_the_start(make_agent, settings):
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    data_dir = Path.home() / f".hermie-test-room-{uuid.uuid4().hex[:8]}"
    try:
        probe = lambda m, info: ModelResponse(parts=[ToolCallPart(
            "run_command", {"command": f"ls {next(settings.connector_rooms_dir.iterdir())}"})])
        ex = Script([probe], final=final())
        agent = make_agent(FakeJudge(task="repetitive"), executor=ex, connectors=[FakeConnector()],
                           data_dir=data_dir)
        await agent.run("revenue?", business=True)
        from .test_web import tool_returns
        assert tool_returns(ex)[0].startswith("exit_code=0")
        assert agent.session.sandbox.room is None
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)
