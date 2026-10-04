"""McpSession against the in-repo fake server: lazy start, serialised requests, timeout kill + restart, clean close."""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from hermie.connectors.mcp_session import McpSession  # noqa: E402

FAKE = str(Path(__file__).parent / "fake_mcp_server.py")


def make(tmp_path, **env):
    base = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "MARKER": "fixed"}
    base.update(env)
    return McpSession([sys.executable, FAKE], base, tmp_path, tmp_path / "fake.stderr.log")


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
    monkeypatch.setenv("SECRET_FOR_TEST", "leak")
    s = make(tmp_path)
    data = json.loads(text(await s.request("call_tool", "echo_env", {}, timeout=30)))
    await s.aclose()
    assert "SECRET_FOR_TEST" not in data["env"] and data["env"]["MARKER"] == "fixed"
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
