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
        self.cwds: list = []

    async def __call__(self, argv, env, timeout, cwd=None):
        self.calls.append((argv, env))
        self.cwds.append(cwd)
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
    for bad in (["--output", "x.tsv"], ["--output=x"], ["--file", "p.json"], ["--decompress"], ["--report-file", "r"],
                ["-file", "x"], ["-output=x"], ["-decompress"], ["-output-dir", "d"]):
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
    async def slow(argv, env, timeout, cwd):
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
    ok = make(sync_runner=lambda argv, env, t, cwd: (0, json.dumps({"credentials": [{"name": "Main"}]}), ""))
    assert ok.status().state == "ready"
    none = make(sync_runner=lambda argv, env, t, cwd: (0, json.dumps({"credentials": []}), ""))
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
        subs, in_subs = [], False
        for line in text.split("\n"):
            if line.startswith("SUBCOMMANDS"):
                in_subs = True
            elif in_subs and line and not line.startswith(" "):
                break
            elif in_subs and line.strip():
                subs.append(line.split()[0])
        from hermie.connectors.asc import WRITE_VERBS
        extra = [x for x in subs if x not in WRITE_VERBS]
        assert not extra, f"asc {' '.join(path)} has subcommands {extra} not in WRITE_VERBS"


async def test_app_name_resolved_to_id(tmp_path):
    runner = FakeRunner()
    r = await tool(make(runner), "asc").call({"args": ["reviews", "list", "--app", "alpha notes"]}, Ctx(tmp_path))
    argv = runner.calls[-1][0]
    assert r.ok and argv[argv.index("--app") + 1] == "111"


async def test_bundle_id_and_apps_view_flag(tmp_path):
    runner = FakeRunner()
    await tool(make(runner), "asc").call({"args": ["apps", "view", "--id", "com.x.beta"]}, Ctx(tmp_path))
    argv = runner.calls[-1][0]
    assert argv[argv.index("--id") + 1] == "222"


async def test_catalog_cached_for_a_day(tmp_path):
    runner = FakeRunner()
    conn, ctx = make(runner), Ctx(tmp_path)
    for _ in range(3):
        await tool(conn, "asc").call({"args": ["reviews", "list", "--app", "Alpha"]}, ctx)
    assert sum(command_path(a[1:]) == ("apps", "list") for a, _ in runner.calls) == 1
    ctx.state()["catalog"]["ts"] -= 2 * 24 * 3600
    await tool(conn, "asc").call({"args": ["reviews", "list", "--app", "Alpha"]}, ctx)
    assert sum(command_path(a[1:]) == ("apps", "list") for a, _ in runner.calls) == 2


async def test_unknown_name_refreshes_then_asks(tmp_path):
    runner = FakeRunner()
    ctx = Ctx(tmp_path, choices=["Beta Fit"])
    r = await tool(make(runner), "asc").call({"args": ["reviews", "list", "--app", "Gamma"]}, ctx)
    assert ctx.asked == [("Which app?", ["Alpha Notes", "Beta Fit"])]
    assert r.ok and runner.calls[-1][0][runner.calls[-1][0].index("--app") + 1] == "222"
    assert ctx.session_state()["app"] == "222"
    assert sum(command_path(a[1:]) == ("apps", "list") for a, _ in runner.calls) == 2   # refreshed once


async def test_ambiguous_name_offers_only_matches(tmp_path):
    apps = {"data": [{"id": "1", "attributes": {"name": "Notes Pro", "bundleId": "a"}},
                     {"id": "2", "attributes": {"name": "Notes Lite", "bundleId": "b"}},
                     {"id": "3", "attributes": {"name": "Fit", "bundleId": "c"}}]}
    runner = FakeRunner({("apps", "list"): (0, json.dumps(apps), "")})
    ctx = Ctx(tmp_path, choices=["Notes Lite"])
    await tool(make(runner), "asc").call({"args": ["reviews", "list", "--app", "notes"]}, ctx)
    assert ctx.asked[0][1] == ["Notes Pro", "Notes Lite"]


async def test_missing_app_uses_session_default_then_single_app(tmp_path):
    runner = FakeRunner()
    ctx = Ctx(tmp_path)
    ctx.session_state()["app"] = "222"
    await tool(make(runner), "asc").call({"args": ["reviews", "list"]}, ctx)
    argv = runner.calls[-1][0]
    assert argv[argv.index("--app") + 1] == "222"
    single = {"data": [{"id": "9", "attributes": {"name": "Only", "bundleId": "o"}}]}
    runner2 = FakeRunner({("apps", "list"): (0, json.dumps(single), "")})
    ctx2 = Ctx(tmp_path)
    await tool(make(runner2), "asc").call({"args": ["reviews", "list"]}, ctx2)
    assert "9" in runner2.calls[-1][0] and ctx2.asked == []


async def test_headless_ambiguity_lists_apps(tmp_path):
    runner = FakeRunner()
    r = await tool(make(runner), "asc").call({"args": ["reviews", "list"]}, Ctx(tmp_path))   # choose -> None
    assert not r.ok and "Alpha Notes" in r.preview and "Beta Fit" in r.preview
    assert all(command_path(a[1:]) != ("reviews", "list") for a, _ in runner.calls)


async def test_vendor_asked_once_and_sent_by_env(tmp_path):
    runner = FakeRunner()
    conn, ctx = make(runner), Ctx(tmp_path, answers=["87654321"])
    await tool(conn, "asc").call({"args": ["analytics", "compare", "--source", "sales", "--from", "2026-09-01",
                                           "--to", "2026-09-08"]}, ctx)
    await tool(conn, "asc").call({"args": ["analytics", "compare", "--source", "sales", "--from", "2026-09-01",
                                           "--to", "2026-09-08"]}, ctx)
    assert len([a for a in ctx.asked if not a[1]]) == 1
    assert ctx.state()["vendor"] == "87654321"
    assert runner.calls[-1][1]["ASC_VENDOR_NUMBER"] == "87654321"


async def test_vendor_refused_when_not_a_number(tmp_path):
    runner = FakeRunner()
    ctx = Ctx(tmp_path, answers=["no idea"])
    r = await tool(make(runner), "asc").call({"args": ["finance", "reports", "--date", "2026-09"]}, ctx)
    assert not r.ok and "vendor number" in r.preview and "vendor" not in ctx.state()
    assert all(command_path(a[1:]) != ("finance", "reports") for a, _ in runner.calls)


async def test_ads_org_discovered_single_and_chosen_multi(tmp_path):
    one = FakeRunner({("ads", "acls", "list"): (0, json.dumps({"data": [{"orgId": 42, "orgName": "Main"}]}), "")})
    ctx = Ctx(tmp_path)
    await tool(make(one), "asc").call({"args": ["ads", "campaigns", "list"]}, ctx)
    assert ctx.state()["ads_org"] == "42" and one.calls[-1][1]["ASC_ADS_ORG_ID"] == "42"
    two = FakeRunner({("ads", "acls", "list"): (0, json.dumps([{"orgId": 1, "orgName": "A"},
                                                               {"orgId": 2, "orgName": "B"}]), "")})
    ctx2 = Ctx(tmp_path, choices=["B"])
    await tool(make(two), "asc").call({"args": ["ads", "campaigns", "list"]}, ctx2)
    assert ctx2.state()["ads_org"] == "2"



async def test_interleaved_write_verbs_and_bare_dashdash_refused(tmp_path):
    runner = FakeRunner()
    conn = make(runner)
    for args in (["analytics", "requests", "--paginate", "delete", "--request-id", "R", "--confirm"],
                 ["ads", "campaigns", "--limit", "5", "delete"],
                 ["ads", "campaigns", "list", "Pause"],
                 ["reviews", "list", "--", "--output", "x"],
                 ["ads", "campaigns", "find", "--file", "x"]):
        r = await tool(conn, "asc").call({"args": args}, Ctx(tmp_path))
        assert not r.ok, args
    assert runner.calls == []


async def test_report_flag_allowed_and_find_not_in_allowlist(tmp_path):
    assert not any(p[-1] == "find" for p in ALLOWED)
    runner = FakeRunner()
    ctx = Ctx(tmp_path)
    ctx.state()["ads_org"] = "1"
    r = await tool(make(runner), "asc").call({"args": ["ads", "impression-share-reports", "view", "--report", "7"]}, ctx)
    assert r.ok


async def test_single_dash_app_flag_is_resolved(tmp_path):
    runner = FakeRunner()
    await tool(make(runner), "asc").call({"args": ["reviews", "list", "-app", "alpha notes"]}, Ctx(tmp_path))
    argv = runner.calls[-1][0]
    assert argv.count("--app") == 1 and "-app" not in argv and argv[argv.index("--app") + 1] == "111"


async def test_runner_oserror_and_ads_hint_not_for_downloads(tmp_path):
    async def boom(argv, env, timeout, cwd):
        raise OSError("no such file")
    r = await tool(make(boom), "asc").call({"args": ["finance", "regions"]}, Ctx(tmp_path))
    assert not r.ok and "OSError" in r.preview
    runner = FakeRunner({("finance", "regions"): (1, "", "downloads: credentials expired")})
    r = await tool(make(runner), "asc").call({"args": ["finance", "regions"]}, Ctx(tmp_path))
    assert not r.ok and "asc ads auth login" not in r.preview


def test_status_non_dict_json_is_error():
    assert make(sync_runner=lambda argv, env, t, cwd: (0, "[]", "")).status().state == "error"


async def test_own_flags_come_right_after_the_path(tmp_path):
    runner = FakeRunner({("finance", "reports"): lambda argv: (Path(argv[argv.index("--output") + 1]).write_text("A\n1\n"), (0, "{}", ""))[1]})
    ctx = Ctx(tmp_path)
    ctx.state()["vendor"] = "123"
    r = await tool(make(runner), "asc").call({"args": ["finance", "reports", "--pretty", "x"]}, ctx)
    argv = runner.calls[-1][0]
    assert r.ok and argv[1:4] == ["finance", "reports", "--output"] and argv.index("--pretty") > argv.index("--output-format")
    runner2 = FakeRunner()
    ctx2 = Ctx(tmp_path)
    ctx2.session_state()["app"] = "111"
    await tool(make(runner2), "asc").call({"args": ["analytics", "request", "--pretty", "x"]}, ctx2)
    argv = runner2.calls[-1][0]
    assert argv.index("--reuse-existing") < argv.index("--pretty")
    assert argv.index("--app") < argv.index("--pretty")


async def test_next_only_apple_hosts(tmp_path):
    runner = FakeRunner()
    conn = make(runner)
    for bad in (["--next", "https://evil.example/x"], ["-next=http://api.appstoreconnect.apple.com/x"],
                ["--next=https://api.appstoreconnect.apple.com.evil.example/x"]):
        r = await tool(conn, "asc").call({"args": ["apps", "list", *bad]}, Ctx(tmp_path))
        assert not r.ok and "--next" in r.preview
    assert runner.calls == []
    for good in ("https://api.appstoreconnect.apple.com/v1/apps?cursor=x", "https://api.searchads.apple.com/api/v5/x"):
        r = await tool(conn, "asc").call({"args": ["apps", "list", "--next", good]}, Ctx(tmp_path))
        assert r.ok


async def test_org_and_help_runner_errors(tmp_path):
    async def boom(argv, env, timeout, cwd):
        raise OSError("gone")
    conn = make(boom)
    r = await tool(conn, "asc").call({"args": ["ads", "campaigns", "list"]}, Ctx(tmp_path))
    assert not r.ok and "OSError" in r.preview
    r = await tool(conn, "asc_help").call({"command": "reviews list"}, Ctx(tmp_path))
    assert not r.ok and "OSError" in r.preview


async def test_reuse_existing_cannot_be_overridden(tmp_path):
    runner = FakeRunner()
    conn = make(runner)
    for bad in (["--reuse-existing=false"], ["-reuse-existing=false"], ["--reuse-existing", "false"]):
        r = await tool(conn, "asc").call({"args": ["analytics", "request", "--app", "alpha notes", *bad]},
                                         Ctx(tmp_path))
        assert not r.ok and "reuse-existing" in r.preview
    assert runner.calls == []


async def test_asc_runs_in_a_hermie_owned_directory(tmp_path):
    """asc reads ./.asc/config.json from its working directory, which would let a planted workspace file override
    the keychain credentials: every asc call runs in a Hermie-owned directory instead."""
    runner = FakeRunner({("finance", "regions"): (0, "[]", "")})
    home = tmp_path / "connectors"
    conn = make(runner, cwd=home)
    await tool(conn, "asc").call({"args": ["finance", "regions"]}, Ctx(tmp_path))
    await tool(conn, "asc").call({"args": ["reviews", "list", "--app", "Alpha Notes"]}, Ctx(tmp_path))
    await tool(conn, "asc_help").call({"command": "reviews list"}, Ctx(tmp_path))
    assert runner.cwds and all(c == home for c in runner.cwds) and home.is_dir()
    seen = []
    conn = make(runner, cwd=home, sync_runner=lambda a, e, t, cwd=None: seen.append(cwd) or (0, "{}", ""))
    conn.binary = Path(__file__)
    conn.status()
    assert seen == [home]


async def test_default_runners_honour_cwd(tmp_path):
    from hermie.connectors.asc import _run, _run_sync
    code, out, _ = await _run(["/bin/pwd"], {}, 5, tmp_path)
    assert code == 0 and Path(out.strip()).resolve() == tmp_path.resolve()
    code, out, _ = _run_sync(["/bin/pwd"], {}, 5, tmp_path)
    assert code == 0 and Path(out.strip()).resolve() == tmp_path.resolve()
