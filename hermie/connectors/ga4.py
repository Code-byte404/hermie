"""GA4 through Google's official analytics-mcp server (read-only: it requests only the analytics.readonly scope).

Runs as `uvx analytics-mcp==<pinned>` in uv's isolated environment, outside the sandbox, with Application Default
Credentials of the user's Google account. The model names properties; this module resolves names to property IDs
from a cached account summary, so the user never types an ID."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import time
from pathlib import Path
from typing import Callable, Optional

from .base import ConnectorContext, Status
from .mcp import McpConnector, McpServerSpec
from .mcp_session import McpSession

GA4_SERVER_VERSION = "0.7.0"
GA4_TOOLS = frozenset({"get_account_summaries", "get_property_details", "list_google_ads_links",
                       "list_property_annotations", "run_report", "run_realtime_report", "run_funnel_report",
                       "run_conversions_report", "get_custom_dimensions_and_metrics"})
CATALOG_TTL_S = 24 * 3600
LOGIN_CMD = ("gcloud auth application-default login --scopes="
             "https://www.googleapis.com/auth/analytics.readonly,https://www.googleapis.com/auth/cloud-platform")

GA4_INSTRUCTIONS = """\
Tools `ga4` and `ga4_help` read the user's Google Analytics 4 data (read-only).
- Call ga4(tool="run_report", arguments={...}). Name the property with property_id="<property name>"; Hermie resolves
  the ID and asks the user when unclear. Never ask the user for a property ID. get_account_summaries lists properties.
- run_report arguments use snake_case: property_id, date_ranges=[{"start_date": "7daysAgo", "end_date": "yesterday"}],
  dimensions=["date"], metrics=["sessions"], optional dimension_filter, metric_filter, order_bys, limit.
  Common dimensions: date, country, city, sessionDefaultChannelGroup, sessionSource, pagePath, landingPage, deviceCategory.
  Common metrics: sessions, activeUsers, newUsers, screenPageViews, engagementRate, conversions, totalRevenue.
- Realtime: run_realtime_report. Custom fields: get_custom_dimensions_and_metrics. Unsure: ga4_help(tool="run_report").
- The full result is saved to a read-only file whose path the tool returns; compute totals and changes from it with
  python in run_command. Write intermediate files under $TMPDIR, not the workspace."""


def find_uvx(configured: str) -> Optional[Path]:
    candidates = [configured] if configured else []
    candidates += [shutil.which("uvx") or "", str(Path.home() / ".local/bin/uvx"), "/opt/homebrew/bin/uvx"]
    for c in candidates:
        if c and Path(c).is_file():
            return Path(c)
    return None


def ga4_status(uvx: Optional[Path], adc_path: Path, mcp_available: bool) -> tuple[Status, str]:
    if uvx is None:
        return Status("missing", "uv is not installed (uvx not found); install uv or set UVX_PATH"), ""
    if not mcp_available:
        return Status("missing", "the MCP client is not installed: pip install -e '.[mcp]' (hermie[mcp])"), ""
    try:
        data = json.loads(Path(adc_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Status("not_authenticated", f"no Google credentials at {adc_path}: run `{LOGIN_CMD}`"), ""
    project = str(data.get("quota_project_id") or "") if isinstance(data, dict) else ""
    if not project:
        return Status("not_authenticated", "the Google credentials have no quota project: run "
                      "`gcloud auth application-default set-quota-project <project with the Analytics APIs enabled>`"), ""
    return Status("ready"), project


def parse_summaries(text: str) -> list[dict]:
    try:
        data = json.loads(text)
    except ValueError:
        return []
    if isinstance(data, dict) and "result" in data:
        data = data["result"]
    out = []
    for acc in data if isinstance(data, list) else []:
        if not isinstance(acc, dict):
            continue
        for p in acc.get("property_summaries") or []:
            pid = str(p.get("property", "")).removeprefix("properties/")
            if pid:
                out.append({"id": pid, "name": str(p.get("display_name") or pid),
                            "account": str(acc.get("display_name") or "")})
    return out


def resolve_property(value: str, props: list[dict]) -> list[dict]:
    v = str(value).strip().lower().removeprefix("properties/")
    for key in ("id", "name"):
        hits = [p for p in props if p[key].lower() == v]
        if hits:
            return hits
    return [p for p in props if v and v in p["name"].lower()]


class Ga4Connector(McpConnector):
    def __init__(self, *, uvx: Optional[Path], adc_path: Path, cwd: Path, timeout: float, preview_chars: int,
                 session_factory: Optional[Callable[[McpServerSpec], McpSession]] = None,
                 mcp_available: Optional[bool] = None, command: Optional[list[str]] = None):
        if mcp_available is None:
            mcp_available = importlib.util.find_spec("mcp") is not None
        self._status, project = ga4_status(uvx, adc_path, mcp_available)
        path_dirs = [str(uvx.parent)] if uvx else []
        path_dirs += [str(Path.home() / ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
        env = {"PATH": ":".join(dict.fromkeys(path_dirs)), "HOME": str(Path.home()),
               "USER": os.environ.get("USER", ""), "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
               "LANG": "en_US.UTF-8", "DO_NOT_TRACK": "1",
               "GOOGLE_APPLICATION_CREDENTIALS": str(adc_path), "GOOGLE_CLOUD_PROJECT": project}
        spec = McpServerSpec(name="ga4", title="Google Analytics 4",
                             command=command or [str(uvx), f"analytics-mcp=={GA4_SERVER_VERSION}"],
                             env=env, allow_tools=GA4_TOOLS, instructions=GA4_INSTRUCTIONS, cwd=Path(cwd))
        super().__init__(spec, timeout=timeout, preview_chars=preview_chars, session_factory=session_factory)

    def status(self) -> Status:
        return self._status

    async def properties(self, state: dict, refresh: bool = False) -> list[dict]:
        cat = state.get("catalog")
        if not refresh and cat and time.time() - cat.get("ts", 0) < CATALOG_TTL_S:
            return cat["properties"]
        result = await self.request("call_tool", "get_account_summaries", {})
        text = "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")
        if getattr(result, "isError", False):
            raise RuntimeError("get_account_summaries failed")
        props = parse_summaries(text)
        state["catalog"] = {"ts": time.time(), "properties": props}
        return props

    async def prepare_args(self, tool: str, args: dict, ctx: ConnectorContext) -> tuple[dict, Optional[str]]:
        if tool == "get_account_summaries":
            return args, None
        state = ctx.state()
        try:
            props = await self.properties(state)
        except Exception as e:
            return args, f"Could not list your GA4 properties ({type(e).__name__})."
        if not props:
            return args, "No GA4 properties are visible to this Google account."
        value = args.get("property_id")
        if value not in (None, ""):
            hits = resolve_property(str(value), props)
            if len(hits) != 1:
                try:
                    props = await self.properties(state, refresh=True)
                except Exception as e:
                    return args, f"Could not list your GA4 properties ({type(e).__name__})."
                hits = resolve_property(str(value), props)
            if len(hits) == 1:
                return {**args, "property_id": hits[0]["id"]}, None
            options = hits or props
        else:
            default = ctx.session_state().get("property")
            if default and any(p["id"] == default for p in props):
                return {**args, "property_id": default}, None
            if len(props) == 1:
                return {**args, "property_id": props[0]["id"]}, None
            options = props
        labels = [f"{p['name']} ({p['account']})" for p in options]
        choice = await ctx.choose("Which GA4 property?", labels)
        if choice is None or choice not in labels:
            return args, ("No GA4 property selected. Your properties: " + ", ".join(p["name"] for p in props)
                          + ". Name one of them with property_id.")
        prop = options[labels.index(choice)]
        ctx.session_state()["property"] = prop["id"]
        return {**args, "property_id": prop["id"]}, None
