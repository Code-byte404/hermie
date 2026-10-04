"""McpConnector against the fake server: allowlist, hook, room output, errors, help."""
import asyncio
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from hermie.connectors.mcp import McpConnector, McpServerSpec  # noqa: E402

FAKE = str(Path(__file__).parent / "fake_mcp_server.py")
ALLOW = frozenset({"run_report", "get_account_summaries", "slow_tool", "fail_tool"})


class Ctx:
    def __init__(self, room):
        self.room, self.n, self._state, self._session = room, 0, {}, {}

    async def choose(self, prompt, options):
        return None

    async def ask(self, prompt):
        return None

    def room_path(self, name):
        self.n += 1
        return self.room / f"{self.n:03d}-{name}"

    def state(self):
        return self._state

    def session_state(self):
        return self._session


def spec(tmp_path, allow=ALLOW):
    return McpServerSpec(name="fake", title="Fake analytics", command=[sys.executable, FAKE],
                         env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}, allow_tools=allow,
                         instructions="FAKE_SHEET", cwd=tmp_path)


def tool(conn, name):
    return next(t for t in conn.tools() if t.name == name)


async def test_tools_and_schema(tmp_path):
    conn = McpConnector(spec(tmp_path), timeout=30, preview_chars=4000)
    names = [t.name for t in conn.tools()]
    assert names == ["fake", "fake_help"]
    assert tool(conn, "fake").parameters["properties"]["tool"]["enum"] == sorted(ALLOW)
    assert conn.status().state == "ready" and conn.instructions() == "FAKE_SHEET"


async def test_no_process_until_first_call(tmp_path):
    conn = McpConnector(spec(tmp_path), timeout=30, preview_chars=4000)
    conn.tools(), conn.status(), conn.instructions()
    assert conn._session is None or not conn._session.running


async def test_call_saves_to_room_and_previews(tmp_path):
    conn = McpConnector(spec(tmp_path), timeout=30, preview_chars=4000)
    r = await tool(conn, "fake").call({"tool": "run_report", "arguments": {"property_id": "111"}}, Ctx(tmp_path))
    await conn.aclose()
    assert r.ok and r.label == "run_report" and "SESSIONS 4242.17" in r.preview
    assert json.loads(r.files[0].read_text())["property"] == "111"


async def test_not_allowed_tool_refused_without_starting(tmp_path):
    conn = McpConnector(spec(tmp_path), timeout=30, preview_chars=4000)
    r = await tool(conn, "fake").call({"tool": "write_tool", "arguments": {}}, Ctx(tmp_path))
    assert not r.ok and r.label == "refused" and r.preview.startswith("Refused") and "run_report" in r.preview
    assert conn._session is None or not conn._session.running


async def test_arguments_must_be_an_object(tmp_path):
    conn = McpConnector(spec(tmp_path), timeout=30, preview_chars=4000)
    r = await tool(conn, "fake").call({"tool": "run_report", "arguments": "property 1"}, Ctx(tmp_path))
    assert not r.ok and "object" in r.preview


async def test_server_error_is_reported(tmp_path):
    conn = McpConnector(spec(tmp_path), timeout=30, preview_chars=4000)
    r = await tool(conn, "fake").call({"tool": "fail_tool", "arguments": {}}, Ctx(tmp_path))
    await conn.aclose()
    assert not r.ok and "fail_tool failed" in r.preview and "upstream API said no" in r.preview
    assert "Traceback" not in r.preview


async def test_timeout_message(tmp_path):
    conn = McpConnector(spec(tmp_path), timeout=1, preview_chars=4000)
    r = await tool(conn, "fake").call({"tool": "slow_tool", "arguments": {"seconds": 30}}, Ctx(tmp_path))
    assert not r.ok and "timed out" in r.preview
    r = await tool(conn, "fake").call({"tool": "run_report", "arguments": {"property_id": "1"}}, Ctx(tmp_path))
    await conn.aclose()
    assert r.ok


async def test_prepare_args_hook(tmp_path):
    class Hooked(McpConnector):
        async def prepare_args(self, tool, args, ctx):
            if tool == "run_report" and args.get("property_id") == "bad":
                return args, "No such property"
            return {**args, "property_id": "999"}, None
    conn = Hooked(spec(tmp_path), timeout=30, preview_chars=4000)
    r = await tool(conn, "fake").call({"tool": "run_report", "arguments": {"property_id": "bad"}}, Ctx(tmp_path))
    assert not r.ok and r.preview == "No such property"
    r = await tool(conn, "fake").call({"tool": "run_report", "arguments": {"property_id": "x"}}, Ctx(tmp_path))
    await conn.aclose()
    assert '"999"' in r.files[0].read_text()


async def test_help_shows_schema_and_refuses_unknown(tmp_path):
    conn = McpConnector(spec(tmp_path), timeout=30, preview_chars=4000)
    r = await tool(conn, "fake_help").call({"tool": "run_report"}, Ctx(tmp_path))
    assert r.ok and "property_id" in r.preview
    r = await tool(conn, "fake_help").call({"tool": "write_tool"}, Ctx(tmp_path))
    await conn.aclose()
    assert not r.ok and "run_report" in r.preview


async def test_preview_is_capped(tmp_path):
    conn = McpConnector(spec(tmp_path), timeout=30, preview_chars=60)
    r = await tool(conn, "fake").call({"tool": "get_account_summaries", "arguments": {}}, Ctx(tmp_path))
    await conn.aclose()
    assert len(r.preview) < 200 and "truncated" in r.preview


class _RaisingSession:
    def __init__(self, exc):
        self.exc = exc

    async def request(self, method, *args, timeout=None):
        raise self.exc

    async def aclose(self):
        pass


def _stub_conn(tmp_path, exc):
    return McpConnector(spec(tmp_path), timeout=30, preview_chars=4000, session_factory=lambda s: _RaisingSession(exc))


async def test_tool_level_mcp_error_reads_as_tool_failure(tmp_path):
    from mcp.shared.exceptions import McpError
    from mcp.types import ErrorData
    conn = _stub_conn(tmp_path, McpError(ErrorData(code=-32602, message="Invalid params: bad date")))
    r = await tool(conn, "fake").call({"tool": "run_report", "arguments": {}}, Ctx(tmp_path))
    assert not r.ok and r.label == "run_report"
    assert r.preview.startswith("run_report failed:") and "bad date" in r.preview and "not reachable" not in r.preview


async def test_connection_error_still_reads_as_outage(tmp_path):
    from hermie.connectors.mcp_session import McpConnectionError
    conn = _stub_conn(tmp_path, McpConnectionError("server died"))
    r = await tool(conn, "fake").call({"tool": "run_report", "arguments": {}}, Ctx(tmp_path))
    assert not r.ok and "not reachable" in r.preview


async def test_help_timeout_message(tmp_path):
    conn = _stub_conn(tmp_path, asyncio.TimeoutError())
    r = await tool(conn, "fake_help").call({"tool": "run_report"}, Ctx(tmp_path))
    assert not r.ok and "timed out" in r.preview


async def test_interpret_error_hook_fails_result_without_saving(tmp_path):
    class Strict(McpConnector):
        def interpret_error(self, tool, text):
            return "bad dimension" if "SESSIONS" in text else None

    conn = Strict(spec(tmp_path), timeout=30, preview_chars=4000)
    r = await tool(conn, "fake").call({"tool": "run_report", "arguments": {"property_id": "1"}}, Ctx(tmp_path))
    await conn.aclose()
    assert not r.ok and r.preview == "run_report failed: bad dimension" and r.label == "run_report" and not r.files
    assert list(tmp_path.glob("0*")) == []
