"""Ga4Connector: status checks, child env, property catalog and resolution (fake server stands in for analytics-mcp)."""
import json
import sys
import time
from pathlib import Path

import pytest

from hermie.connectors.ga4 import (GA4_SERVER_VERSION, GA4_TOOLS, Ga4Connector, find_uvx, ga4_status,
                                   parse_summaries, resolve_property)

FAKE = str(Path(__file__).parent / "fake_mcp_server.py")


def adc(tmp_path, quota="my-proj", content=None):
    p = tmp_path / "adc.json"
    p.write_text(content if content is not None else json.dumps(
        {"type": "authorized_user", "client_id": "x", "refresh_token": "y", "quota_project_id": quota}))
    return p


def test_status_variants(tmp_path):
    uvx = tmp_path / "uvx"
    uvx.write_text("")
    st, _ = ga4_status(None, adc(tmp_path), True)
    assert st.state == "missing" and "uv" in st.reason
    st, _ = ga4_status(uvx, adc(tmp_path), False)
    assert st.state == "missing" and "hermie[mcp]" in st.reason
    st, _ = ga4_status(uvx, tmp_path / "nope.json", True)
    assert st.state == "not_authenticated" and "gcloud auth application-default login" in st.reason
    st, _ = ga4_status(uvx, adc(tmp_path, content="{not json"), True)
    assert st.state == "not_authenticated"
    st, _ = ga4_status(uvx, adc(tmp_path, quota=""), True)
    assert st.state == "not_authenticated" and "set-quota-project" in st.reason
    st, project = ga4_status(uvx, adc(tmp_path), True)
    assert st.state == "ready" and project == "my-proj"


def test_find_uvx_prefers_configured(tmp_path, monkeypatch):
    f = tmp_path / "uvx"
    f.write_text("")
    assert find_uvx(str(f)) == f
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert find_uvx(str(tmp_path / "missing")) in (None, Path.home() / ".local/bin/uvx", Path("/opt/homebrew/bin/uvx"))


def test_spec_command_env_and_allowlist(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_FOR_TEST", "leak")
    uvx = tmp_path / "bin" / "uvx"
    uvx.parent.mkdir()
    uvx.write_text("")
    conn = Ga4Connector(uvx=uvx, adc_path=adc(tmp_path), cwd=tmp_path / "cwd", timeout=30, preview_chars=4000,
                        mcp_available=True)
    s = conn.spec
    assert s.command == [str(uvx), f"analytics-mcp=={GA4_SERVER_VERSION}"]
    assert s.allow_tools == GA4_TOOLS and len(GA4_TOOLS) == 9 and s.cwd == tmp_path / "cwd"
    assert s.env["GOOGLE_CLOUD_PROJECT"] == "my-proj" and s.env["GOOGLE_APPLICATION_CREDENTIALS"] == str(adc(tmp_path))
    assert "SECRET_FOR_TEST" not in s.env and str(uvx.parent) in s.env["PATH"].split(":")
    assert conn.status().state == "ready" and [t.name for t in conn.tools()] == ["ga4", "ga4_help"]


def test_catalog_parses_wrapped_and_bare():
    accounts = [{"account": "accounts/1", "display_name": "Main Co",
                 "property_summaries": [{"property": "properties/111", "display_name": "Alpha Web"}]}]
    want = [{"id": "111", "name": "Alpha Web", "account": "Main Co"}]
    assert parse_summaries(json.dumps({"result": accounts})) == want
    assert parse_summaries(json.dumps(accounts)) == want
    assert parse_summaries("garbage") == []


def test_resolve_property():
    props = [{"id": "111", "name": "Alpha Web", "account": "A"}, {"id": "222", "name": "Alpha App", "account": "A"},
             {"id": "333", "name": "Beta Site", "account": "B"}]
    assert [p["id"] for p in resolve_property("properties/333", props)] == ["333"]
    assert [p["id"] for p in resolve_property("222", props)] == ["222"]
    assert [p["id"] for p in resolve_property("beta site", props)] == ["333"]
    assert [p["id"] for p in resolve_property("alpha", props)] == ["111", "222"]
    assert resolve_property("gamma", props) == []


# ------------------------------------------------------------ against the fake server

class Ctx:
    def __init__(self, room, choices=()):
        self.room, self.choices, self.asked, self.n, self._state, self._session = room, list(choices), [], 0, {}, {}

    async def choose(self, prompt, options):
        self.asked.append((prompt, list(options)))
        return self.choices.pop(0) if self.choices else None

    async def ask(self, prompt):
        return None

    def room_path(self, name):
        self.n += 1
        return self.room / f"{self.n:03d}-{name}"

    def state(self):
        return self._state

    def session_state(self):
        return self._session


def fake_conn(tmp_path):
    pytest.importorskip("mcp")
    uvx = tmp_path / "uvx"
    uvx.write_text("")
    return Ga4Connector(uvx=uvx, adc_path=adc(tmp_path), cwd=tmp_path, timeout=30, preview_chars=4000,
                        mcp_available=True, command=[sys.executable, FAKE])


def call(conn, ctx, tool, **arguments):
    t = next(x for x in conn.tools() if x.name == "ga4")
    return t.call({"tool": tool, "arguments": arguments}, ctx)


async def test_property_name_resolved(tmp_path):
    conn, ctx = fake_conn(tmp_path), Ctx(tmp_path)
    r = await call(conn, ctx, "run_report", property_id="beta site", date_ranges=[], dimensions=[], metrics=[])
    await conn.aclose()
    assert r.ok and json.loads(r.files[0].read_text())["property"] == "333"


async def test_ambiguous_name_asks_and_remembers(tmp_path):
    conn, ctx = fake_conn(tmp_path), Ctx(tmp_path, choices=["Alpha App (Main Co)"])
    r = await call(conn, ctx, "run_report", property_id="alpha")
    assert ctx.asked == [("Which GA4 property?", ["Alpha Web (Main Co)", "Alpha App (Main Co)"])]
    assert r.ok and ctx.session_state()["property"] == "222"
    r = await call(conn, ctx, "run_report")                          # no property: session default, no question
    await conn.aclose()
    assert len(ctx.asked) == 1 and json.loads(r.files[0].read_text())["property"] == "222"


async def test_headless_ambiguity_lists_properties(tmp_path):
    conn, ctx = fake_conn(tmp_path), Ctx(tmp_path)
    r = await call(conn, ctx, "run_report")
    await conn.aclose()
    assert not r.ok and "Alpha Web" in r.preview and "Beta Site" in r.preview


async def test_account_summaries_needs_no_property(tmp_path):
    conn, ctx = fake_conn(tmp_path), Ctx(tmp_path)
    r = await call(conn, ctx, "get_account_summaries")
    await conn.aclose()
    assert r.ok and ctx.asked == []


async def test_catalog_cached_and_refreshed_on_miss(tmp_path):
    conn, ctx = fake_conn(tmp_path), Ctx(tmp_path)
    await call(conn, ctx, "run_report", property_id="Beta Site")
    ts = ctx.state()["catalog"]["ts"]
    await call(conn, ctx, "run_report", property_id="Beta Site")
    assert ctx.state()["catalog"]["ts"] == ts                        # cached
    ctx.state()["catalog"]["ts"] = time.time() - 2 * 24 * 3600
    await call(conn, ctx, "run_report", property_id="Beta Site")
    await conn.aclose()
    assert ctx.state()["catalog"]["ts"] > ts                         # expired -> refreshed


# ------------------------------------------------------------ analytics-mcp reports errors as ordinary text

class _Text:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Result:
    isError = False

    def __init__(self, text):
        self.content = [_Text(text)]


class _FlakySession:
    """Stands in for the server: get_account_summaries fails until `ok` is set."""

    def __init__(self):
        self.ok = False

    async def request(self, method, *args, timeout=None):
        if not self.ok:
            return _Result(json.dumps({"error": "Failed to execute tool 'get_account_summaries': token expired"}))
        return _Result(json.dumps({"result": [{"account": "accounts/1", "display_name": "Main Co", "property_summaries": [
            {"property": "properties/111", "display_name": "Alpha Web"}]}]}))

    async def aclose(self):
        pass


async def test_failed_catalog_is_not_cached_and_recovers(tmp_path):
    uvx = tmp_path / "uvx"
    uvx.write_text("")
    sess = _FlakySession()
    conn = Ga4Connector(uvx=uvx, adc_path=adc(tmp_path), cwd=tmp_path, timeout=30, preview_chars=4000,
                        mcp_available=True, session_factory=lambda s: sess)
    ctx = Ctx(tmp_path)
    args, problem = await conn.prepare_args("run_report", {"property_id": "alpha"}, ctx)
    assert problem and problem.startswith("Could not list your GA4 properties") and "catalog" not in ctx.state()
    sess.ok = True
    args, problem = await conn.prepare_args("run_report", {"property_id": "alpha"}, ctx)
    assert problem is None and args["property_id"] == "111"


async def test_empty_cached_catalog_is_a_miss(tmp_path):
    conn, ctx = fake_conn(tmp_path), Ctx(tmp_path)
    ctx.state()["catalog"] = {"ts": time.time(), "properties": []}
    r = await call(conn, ctx, "run_report", property_id="Beta Site")
    await conn.aclose()
    assert r.ok and json.loads(r.files[0].read_text())["property"] == "333"


async def test_error_text_result_is_a_failure_and_not_saved(tmp_path):
    import dataclasses
    conn, ctx = fake_conn(tmp_path), Ctx(tmp_path)
    conn.spec = dataclasses.replace(conn.spec, allow_tools=GA4_TOOLS | {"error_text_tool"})
    r = await call(conn, ctx, "error_text_tool", property_id="Beta Site")
    await conn.aclose()
    assert not r.ok and r.preview == "error_text_tool failed: bad dimension" and not r.files
    assert list(tmp_path.glob("0*")) == []


def test_env_quota_project_and_proxy_passthrough(tmp_path, monkeypatch):
    for k in ("HTTPS_PROXY", "https_proxy", "SSL_CERT_FILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:3128")
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/ca.pem")
    monkeypatch.setenv("SECRET_FOR_TEST", "leak")
    uvx = tmp_path / "uvx"
    uvx.write_text("")
    env = Ga4Connector(uvx=uvx, adc_path=adc(tmp_path), cwd=tmp_path, timeout=30, preview_chars=4000,
                       mcp_available=True).spec.env
    assert env["GOOGLE_CLOUD_QUOTA_PROJECT"] == "my-proj" and env["GOOGLE_CLOUD_PROJECT"] == "my-proj"
    assert env["HTTPS_PROXY"] == "http://proxy:3128" and env["SSL_CERT_FILE"] == "/etc/ca.pem"
    assert "https_proxy" not in env and "SECRET_FOR_TEST" not in env


def test_parse_summaries_skips_malformed_entries():
    text = json.dumps({"result": [{"display_name": "Main Co", "property_summaries": [
        "junk", None, {"property": "properties/111", "display_name": "Alpha Web"}]}]})
    assert parse_summaries(text) == [{"id": "111", "name": "Alpha Web", "account": "Main Co"}]
