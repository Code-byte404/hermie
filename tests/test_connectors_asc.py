"""AscConnector with a fake runner: never runs the real asc."""
import asyncio
import gzip
import json
import shutil
from pathlib import Path

import pytest

from hermie.connectors.asc import ALLOWED, AscConnector, command_path


class Ctx:
    """A ConnectorContext for tests: scripted answers, a temp data room, in-memory state."""

    def __init__(self, room: Path, choices=(), answers=()):
        self.room, self.choices, self.answers = room, list(choices), list(answers)
        self.asked: list[tuple[str, list[str]]] = []
        self._state, self._session, self.n = {}, {}, 0

    async def choose(self, prompt, options):
        self.asked.append((prompt, list(options)))
        return self.choices.pop(0) if self.choices else None

    async def ask(self, prompt):
        self.asked.append((prompt, []))
        return self.answers.pop(0) if self.answers else None

    def room_path(self, name):
        self.n += 1
        return self.room / f"{self.n:03d}-{name}"

    def state(self):
        return self._state

    def session_state(self):
        return self._session


APPS = {"data": [{"type": "apps", "id": "111", "attributes": {"name": "Alpha Notes", "bundleId": "com.x.alpha"}},
                 {"type": "apps", "id": "222", "attributes": {"name": "Beta Fit", "bundleId": "com.x.beta"}}]}


class FakeRunner:
    """Answers argv by the command path; records (argv, env)."""

    def __init__(self, replies=None):
        self.replies = {("apps", "list"): (0, json.dumps(APPS), ""), **(replies or {})}
        self.calls: list[tuple[list[str], dict]] = []

    async def __call__(self, argv, env, timeout):
        self.calls.append((argv, env))
        path = command_path(argv[1:])
        reply = self.replies.get(path, (0, json.dumps({"data": []}), ""))
        if callable(reply):
            return reply(argv)
        return reply


def make(runner=None, **kw):
    return AscConnector(Path("/opt/homebrew/bin/asc"), timeout=5, preview_chars=kw.pop("preview_chars", 4000),
                        runner=runner or FakeRunner(), **kw)


def tool(conn, name):
    return next(t for t in conn.tools() if t.name == name)


def test_command_path_stops_at_first_flag():
    assert command_path(["reviews", "list", "--app", "x"]) == ("reviews", "list")
    assert command_path(["--help"]) == ()


async def test_write_commands_refused(tmp_path):
    runner = FakeRunner()
    conn = make(runner)
    for args in (["ads", "api", "POST", "/x"], ["apps", "update", "--id", "1"], ["reviews", "respond"],
                 ["analytics", "requests", "delete", "--request-id", "1"], ["publish", "appstore"]):
        r = await tool(conn, "asc").call({"args": args}, Ctx(tmp_path))
        assert not r.ok and r.preview.startswith("Refused")
    assert runner.calls == []


async def test_output_and_file_flags_refused(tmp_path):
    runner = FakeRunner()
    conn = make(runner)
    for bad in (["--output", "x.tsv"], ["--output=x"], ["--file", "p.json"], ["--decompress"], ["--report-file", "r"]):
        r = await tool(conn, "asc").call({"args": ["finance", "regions", *bad]}, Ctx(tmp_path))
        assert not r.ok and "not allowed" in r.preview
    assert runner.calls == []


async def test_json_command_saved_and_previewed(tmp_path):
    runner = FakeRunner({("finance", "regions"): (0, json.dumps([{"code": "US"}, {"code": "ZZ"}]), "")})
    conn = make(runner)
    r = await tool(conn, "asc").call({"args": ["finance", "regions"]}, Ctx(tmp_path))
    argv, env = runner.calls[-1]
    assert argv == ["/opt/homebrew/bin/asc", "finance", "regions", "--output", "json"]
    assert r.ok and r.label == "finance regions" and r.preview.startswith("2 rows")
    assert json.loads(r.files[0].read_text()) == [{"code": "US"}, {"code": "ZZ"}]


async def test_env_is_an_allowlist_with_telemetry_off(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_TOKEN_FOR_TEST", "leak")
    runner = FakeRunner()
    await tool(make(runner), "asc").call({"args": ["finance", "regions"]}, Ctx(tmp_path))
    env = runner.calls[-1][1]
    assert "SECRET_TOKEN_FOR_TEST" not in env
    assert env["DO_NOT_TRACK"] == "1" and env["ASC_TELEMETRY_DISABLED"] == "1"
    assert set(env) <= {"PATH", "HOME", "USER", "TMPDIR", "LANG", "DO_NOT_TRACK", "ASC_TELEMETRY_DISABLED",
                        "ASC_VENDOR_NUMBER", "ASC_ANALYTICS_VENDOR_NUMBER", "ASC_ADS_ORG_ID"}


async def test_file_command_gets_room_output(tmp_path):
    def write(argv):
        Path(argv[argv.index("--output") + 1]).write_text("Title\tUnits\nAlpha\t5\n")
        return 0, "{}", ""
    runner = FakeRunner({("finance", "reports"): write})
    conn = make(runner)
    ctx = Ctx(tmp_path, answers=["12345678"])
    r = await tool(conn, "asc").call({"args": ["finance", "reports", "--report-type", "FINANCIAL", "--region", "US",
                                               "--date", "2026-09"]}, ctx)
    argv = runner.calls[-1][0]
    assert "--decompress" in argv and argv[argv.index("--output-format") + 1] == "json"
    assert Path(argv[argv.index("--output") + 1]).parent == tmp_path
    assert r.ok and r.preview.startswith("1 rows; columns: Title\tUnits")


async def test_file_command_gz_fallback(tmp_path):
    def write_gz(argv):
        out = Path(argv[argv.index("--output") + 1])
        with gzip.open(str(out) + ".gz", "wt") as f:
            f.write("A\tB\n1\t2\n")
        return 0, "{}", ""
    runner = FakeRunner({("finance", "reports"): write_gz})
    ctx = Ctx(tmp_path, answers=["12345678"])
    r = await tool(make(runner), "asc").call({"args": ["finance", "reports", "--date", "2026-09"]}, ctx)
    assert r.ok and r.files[0].read_text() == "A\tB\n1\t2\n" and r.preview.startswith("1 rows")


async def test_nonzero_exit_and_ads_hint(tmp_path):
    runner = FakeRunner({("ads", "acls", "list"): (1, "", "Error: ads: default credentials not found")})
    r = await tool(make(runner), "asc").call({"args": ["ads", "acls", "list"]}, Ctx(tmp_path))
    assert not r.ok and "exit 1" in r.preview and "asc ads auth login" in r.preview


async def test_timeout(tmp_path):
    async def slow(argv, env, timeout):
        raise asyncio.TimeoutError
    r = await tool(make(slow), "asc").call({"args": ["finance", "regions"]}, Ctx(tmp_path))
    assert not r.ok and "timed out" in r.preview and "narrower" in r.preview


async def test_preview_is_capped(tmp_path):
    big = json.dumps([{"k": "v" * 100}] * 200)
    runner = FakeRunner({("finance", "regions"): (0, big, "")})
    r = await tool(make(runner, preview_chars=500), "asc").call({"args": ["finance", "regions"]}, Ctx(tmp_path))
    assert len(r.preview) < 700 and len(r.files[0].read_text()) == len(big)


async def test_analytics_request_always_reuses(tmp_path):
    runner = FakeRunner()
    ctx = Ctx(tmp_path)
    ctx.session_state()["app"] = "111"
    await tool(make(runner), "asc").call({"args": ["analytics", "request", "--access-type", "ONGOING"]}, ctx)
    assert "--reuse-existing" in runner.calls[-1][0]


async def test_asc_help_allowed_and_search(tmp_path):
    runner = FakeRunner({("reviews", "list"): (0, "USAGE asc reviews list", ""),
                         ("search",): (0, '{"results": []}', "")})
    conn = make(runner)
    r = await tool(conn, "asc_help").call({"command": "reviews list"}, Ctx(tmp_path))
    assert r.ok and runner.calls[-1][0][-1] == "--help"
    r = await tool(conn, "asc_help").call({"command": "download sales"}, Ctx(tmp_path))
    assert runner.calls[-1][0][1:3] == ["search", "download sales"]
    r = await tool(conn, "asc_help").call({"command": "ads api"}, Ctx(tmp_path))
    assert not r.ok


def test_status(tmp_path):
    ok = make(sync_runner=lambda argv, env, t: (0, json.dumps({"credentials": [{"name": "Main"}]}), ""))
    assert ok.status().state == "ready"
    none = make(sync_runner=lambda argv, env, t: (0, json.dumps({"credentials": []}), ""))
    st = none.status()
    assert st.state == "not_authenticated" and "asc auth login" in st.reason
    missing = AscConnector(tmp_path / "nope", runner=FakeRunner())
    assert missing.status().state == "missing"


@pytest.mark.skipif(shutil.which("asc") is None, reason="asc not installed")
def test_allowlist_exists_in_installed_asc():
    import subprocess
    for path in sorted(ALLOWED):
        out = subprocess.run(["asc", *path, "--help"], capture_output=True, text=True, timeout=30,
                             env={"PATH": "/usr/bin:/bin:/opt/homebrew/bin", "HOME": str(Path.home()),
                                  "DO_NOT_TRACK": "1"})
        text = out.stdout + out.stderr
        assert f"asc {' '.join(path)}" in text, f"asc {' '.join(path)} does not exist in the installed asc"
