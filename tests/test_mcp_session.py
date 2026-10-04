"""McpSession against the in-repo fake server: lazy start, serialised requests, timeout kill + restart, clean close."""
import asyncio
import json
import os
import signal
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from hermie.connectors import mcp_session  # noqa: E402
from hermie.connectors.mcp_session import McpSession  # noqa: E402

FAKE = str(Path(__file__).parent / "fake_mcp_server.py")


def make(tmp_path, **env):
    base = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "MARKER": "fixed"}
    base.update(env)
    return McpSession([sys.executable, FAKE], base, tmp_path, tmp_path / "fake.stderr.log")


# set by the child Python itself (PEP 538 C-locale coercion when no locale is given), not passed by Hermie
_OS_ADDED_ENV = {"LC_CTYPE"}


def text(result) -> str:
    return "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")


async def test_lazy_start_and_call(tmp_path):
    s = make(tmp_path)
    assert not s.running
    tools = await s.request("list_tools", timeout=30)
    assert s.running and "run_report" in {t.name for t in tools.tools}
    res = await s.request("call_tool", "run_report", {"property_id": "111"}, timeout=30)
    assert "SESSIONS 4242.17" in text(res)
    await s.aclose()
    assert not s.running


async def test_env_and_cwd_are_exactly_given(tmp_path, monkeypatch):
    """The child gets the given env plus mcp's own inherited defaults (HOME, LOGNAME, PATH, SHELL, TERM, USER;
    given values win) and nothing else from Hermie's environment."""
    from mcp.client.stdio import DEFAULT_INHERITED_ENV_VARS
    monkeypatch.setenv("SECRET_FOR_TEST", "leak")
    s = make(tmp_path)
    data = json.loads(text(await s.request("call_tool", "echo_env", {}, timeout=30)))
    await s.aclose()
    assert "SECRET_FOR_TEST" not in data["env"] and data["env"]["MARKER"] == "fixed"
    allowed = set(s.env) | set(DEFAULT_INHERITED_ENV_VARS) | _OS_ADDED_ENV
    assert set(data["env"]) <= allowed, set(data["env"]) - allowed
    assert data["env"]["HOME"] == str(tmp_path)                    # the given value wins over mcp's default
    assert os.path.realpath(data["cwd"]) == os.path.realpath(tmp_path)


async def test_concurrent_requests_are_serialised(tmp_path):
    s = make(tmp_path)
    a, b = await asyncio.gather(s.request("call_tool", "slow_tool", {"seconds": 0.2}, timeout=30),
                                s.request("call_tool", "run_report", {"property_id": "1"}, timeout=30))
    assert text(a) == "done" and "SESSIONS" in text(b)
    await s.aclose()


async def test_timeout_kills_and_next_call_restarts(tmp_path):
    s = make(tmp_path)
    await s.request("list_tools", timeout=30)    # started first, so the 1 s budget below times out the call itself
    with pytest.raises(asyncio.TimeoutError):
        await s.request("call_tool", "slow_tool", {"seconds": 30}, timeout=1)
    assert not s.running
    res = await s.request("call_tool", "run_report", {"property_id": "1"}, timeout=30)
    assert "SESSIONS" in text(res)
    await s.aclose()


async def test_restart_after_server_exit(tmp_path):
    s = make(tmp_path)
    with pytest.raises(Exception):
        await s.request("call_tool", "exit_now", {}, timeout=10)
    for _ in range(50):
        if not s.running:
            break
        await asyncio.sleep(0.1)
    res = await s.request("call_tool", "run_report", {"property_id": "1"}, timeout=30)
    assert "SESSIONS" in text(res)
    await s.aclose()


async def test_start_failure_raises_and_does_not_hang(tmp_path):
    s = McpSession([sys.executable, str(tmp_path / "missing.py")], {"PATH": "/usr/bin:/bin"}, tmp_path,
                   tmp_path / "x.log")
    with pytest.raises(Exception):
        await s.request("list_tools", timeout=15)
    assert not s.running


async def test_requests_from_different_tasks(tmp_path):
    """A session started in one asyncio task is used and closed from others (Hermie runs each task in its own)."""
    s = make(tmp_path)
    await asyncio.create_task(s.request("list_tools", timeout=30))
    res = await asyncio.create_task(s.request("call_tool", "run_report", {"property_id": "1"}, timeout=30))
    assert "SESSIONS" in text(res)
    await asyncio.create_task(s.aclose())
    assert not s.running


async def test_server_exits_when_parent_closes_pipes(tmp_path):
    """Without aclose the server must still exit on stdin EOF (no orphan after a Hermie crash)."""
    proc = await asyncio.create_subprocess_exec(sys.executable, FAKE, stdin=asyncio.subprocess.PIPE,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    proc.stdin.close()
    await asyncio.wait_for(proc.wait(), 15)


async def _server_pid(s) -> int:
    return json.loads(text(await s.request("call_tool", "echo_env", {}, timeout=30)))["pid"]


async def test_idle_server_death_is_noticed_and_next_request_works(tmp_path):
    """A server killed while idle: `running` turns False on its own and the next request starts a new server."""
    s = make(tmp_path)
    pid = await _server_pid(s)
    os.kill(pid, signal.SIGKILL)
    for _ in range(100):
        if not s.running:
            break
        await asyncio.sleep(0.05)
    assert not s.running
    assert "SESSIONS" in text(await s.request("call_tool", "run_report", {"property_id": "1"}, timeout=30))
    assert await _server_pid(s) != pid
    await s.aclose()


async def test_request_right_after_idle_kill_costs_at_most_one_connection_error(tmp_path):
    s = make(tmp_path)
    os.kill(await _server_pid(s), signal.SIGKILL)
    try:                                         # may race the death: then it fails, but only as a ConnectionError
        await s.request("call_tool", "run_report", {"property_id": "1"}, timeout=30)
    except ConnectionError:
        pass
    assert "SESSIONS" in text(await s.request("call_tool", "run_report", {"property_id": "1"}, timeout=30))
    await s.aclose()


async def test_aclose_during_slow_start_gives_starter_connection_error(tmp_path, monkeypatch):
    """aclose from another task while the first launch is still slow: the starter, never cancelled itself, gets a
    ConnectionError, not CancelledError."""
    monkeypatch.setattr(mcp_session, "_CLOSE_GRACE_S", 0.2)
    s = McpSession(["/bin/sh", "-c", f"sleep 2; exec {sys.executable} {FAKE}"],
                   {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}, tmp_path, tmp_path / "slow.log")
    starter = asyncio.create_task(s.request("list_tools", timeout=30))
    await asyncio.sleep(0.2)
    await s.aclose()
    with pytest.raises(ConnectionError):
        await starter
    assert not starter.cancelled() and not s.running


async def test_queued_request_timeout_leaves_the_call_in_flight_alone(tmp_path):
    s = make(tmp_path)
    await s.request("list_tools", timeout=30)
    long = asyncio.create_task(s.request("call_tool", "slow_tool", {"seconds": 1.5}, timeout=30))
    await asyncio.sleep(0.2)
    with pytest.raises(asyncio.TimeoutError):
        await s.request("call_tool", "run_report", {"property_id": "1"}, timeout=0.3)
    assert s.running
    assert text(await long) == "done"
    assert "SESSIONS" in text(await s.request("call_tool", "run_report", {"property_id": "1"}, timeout=30))
    await s.aclose()


async def test_unusable_errlog_fails_the_start_fast(tmp_path):
    (tmp_path / "file").write_text("x")
    s = McpSession([sys.executable, FAKE], {"PATH": "/usr/bin:/bin"}, tmp_path, tmp_path / "file" / "err.log")
    with pytest.raises(ConnectionError):
        await s.request("list_tools", timeout=15)
    assert not s.running


async def test_errlog_is_appended_and_bounded(tmp_path, monkeypatch):
    log_path = tmp_path / "fake.stderr.log"
    log_path.write_text("output of an earlier Hermie process\n")
    s = make(tmp_path)
    await s.request("list_tools", timeout=30)
    await s.aclose()
    assert "earlier Hermie process" not in log_path.read_text()      # first start in this process: truncated
    with open(log_path, "a") as f:
        f.write("earlier crash\n")
    await s.request("list_tools", timeout=30)                        # a restart within this process appends
    await s.aclose()
    assert "earlier crash\n" in log_path.read_text()
    s2 = make(tmp_path)                                              # another session on the same file too
    await s2.request("list_tools", timeout=30)
    await s2.aclose()
    assert "earlier crash\n" in log_path.read_text()
    monkeypatch.setattr(mcp_session, "_ERRLOG_MAX_BYTES", 5)
    await s.request("list_tools", timeout=30)
    await s.aclose()
    assert "earlier crash" not in log_path.read_text()


async def test_mcp_logger_is_silenced_once_a_session_starts(tmp_path):
    """mcp logs a snippet of non-JSON server stdout via logger.exception; that text must not reach Hermie's log."""
    import logging
    logging.getLogger("mcp").setLevel(logging.NOTSET)
    s = make(tmp_path)
    await s.request("list_tools", timeout=30)
    await s.aclose()
    assert logging.getLogger("mcp").level == logging.CRITICAL


def test_close_grace_fits_hermies_close_bound():
    """Worst case: grace, then mcp's teardown waits for stdin EOF exit and for SIGTERM before SIGKILL."""
    from mcp.client.stdio import PROCESS_TERMINATION_TIMEOUT
    from hermie import core
    assert mcp_session._CLOSE_GRACE_S == 1.0 and core.CLOSE_TIMEOUT_S == 8.0
    assert mcp_session._CLOSE_GRACE_S + 2 * PROCESS_TERMINATION_TIMEOUT + 2 <= core.CLOSE_TIMEOUT_S
