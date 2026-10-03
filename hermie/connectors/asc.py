"""asc connector: App Store Connect + Apple Ads through the installed `asc` CLI.

asc keeps its credentials in the system keychain, which the sandbox denies on purpose, so this runs in the main
process: fixed binary, argv list (no shell), a fixed environment (never os.environ) with asc telemetry off, and a
read-only allowlist of exact command paths. The model names apps; this module resolves names to App Store Connect
IDs from a cached catalog, so the user never types an ID."""
from __future__ import annotations

import asyncio
import gzip
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .base import ConnectorContext, ConnectorResult, ConnectorTool, Status
from .preview import cap_text, preview_json, preview_table

# (argv, env, timeout, cwd): cwd is a Hermie-owned directory, never the workspace (asc reads ./.asc/config.json)
Runner = Callable[[list[str], dict, float, Path], Awaitable[tuple[int, str, str]]]
SyncRunner = Callable[[list[str], dict, float, Path], tuple[int, str, str]]

# Exact command paths (the leading non-flag tokens). Anything else is refused, including `ads api` (raw requests,
# can POST) and every create/update/delete/respond/submit path.
ALLOWED: frozenset[tuple[str, ...]] = frozenset({
    ("apps", "list"), ("apps", "view"),
    ("reviews", "list"), ("reviews", "view"), ("reviews", "ratings"), ("reviews", "summarizations"),
    # ("analytics", "request") creates a report request on App Store Connect (idempotent with --reuse-existing, which
    # Hermie always adds): the one entry that is not a pure read
    ("analytics", "sales"), ("analytics", "compare"), ("analytics", "request"), ("analytics", "requests"),
    ("analytics", "view"), ("analytics", "reports", "view"), ("analytics", "instances", "view"),
    ("analytics", "segments", "view"), ("analytics", "download"),
    ("insights", "weekly"), ("insights", "daily"),
    ("finance", "reports"), ("finance", "regions"),
    *(("ads", r, v) for r in ("campaigns", "ad-groups", "ads", "targeting-keywords") for v in ("list", "view")),
    ("ads", "impression-share-reports", "list"), ("ads", "impression-share-reports", "view"),
    ("ads", "reports", "preset"), ("ads", "acls", "list"), ("ads", "me", "view"),
})
# Commands that write a report file: --output is a file path there; Hermie supplies it in the data room
FILE_COMMANDS = {("analytics", "sales"): ".tsv", ("analytics", "download"): ".csv", ("finance", "reports"): ".tsv"}
# Flags the model may not pass: they write to arbitrary paths, read payload files, or change the output format
# (names without dashes: Go's flag package accepts -file as well as --file)
REFUSED_FLAGS = {"output", "output-format", "decompress", "file", "report-file", "output-dir",
                 "reuse-existing"}   # Hermie adds --reuse-existing itself; a repeated flag keeps its last value
# asc dispatches the next bare word after parent flags as a subcommand (`asc analytics requests --paginate delete`),
# so a path check on the leading tokens is not enough: no write verb may appear anywhere after the path. Taken from
# `asc <path> --help` of every allowlisted path (delete/create/update/pause/resume/create-bulk/...) plus the usual
# write verbs; find/find-org are refused because their only input is --file.
WRITE_VERBS = frozenset({
    "delete", "create", "update", "respond", "respond-batch", "submit", "publish", "release", "pause", "resume",
    "cancel", "remove", "add", "set", "edit", "upload", "invite", "enable", "disable", "rename", "attach", "detach",
    "expire", "revoke", "create-bulk", "delete-bulk", "update-bulk", "apply", "sync", "import", "find", "find-org",
    "remove-beta-testers"})
APP_FLAG = {("apps", "view"): "--id"}   # everywhere else the app flag is --app
APP_REQUIRED = {("apps", "view"), ("reviews", "list"), ("insights", "weekly"), ("insights", "daily"),
                ("analytics", "request"), ("analytics", "requests")}
VENDOR_REQUIRED = {("analytics", "sales"), ("analytics", "compare"), ("finance", "reports"), ("insights", "daily")}
ORGLESS = {("ads", "acls", "list"), ("ads", "me", "view")}
CATALOG_TTL_S = 24 * 3600
NEXT_PREFIXES = ("https://api.appstoreconnect.apple.com/", "https://api.searchads.apple.com/")
ADS_HINT = "Apple Ads is not set up for asc: run `asc ads auth login` in a terminal."

ASC_INSTRUCTIONS = """\
Tools `asc` and `asc_help` read the user's App Store Connect and Apple Ads data (read-only).
- Pass arguments as a list: asc(args=["reviews","list","--app","Alpha Notes","--stars","1","--paginate"]).
- Name apps by their name or bundle id with --app; Hermie resolves the ID and asks the user when unclear. Never ask
  the user for an app ID, vendor number or organization ID: Hermie fills them in.
- Do not pass --output, --decompress or --file: the full result is saved to a read-only file whose path the tool
  returns, with a preview. Compute totals and comparisons from that file with python in run_command.
- Write intermediate files (scripts, CSVs) under $TMPDIR, not the workspace.
- Useful commands: apps list | reviews list --app A [--stars N --territory US --sort -createdDate --paginate]
  | insights weekly --app A --source sales --week YYYY-MM-DD | insights daily --app A --date YYYY-MM-DD
  | analytics compare --source sales --app A --from YYYY-MM-DD --to YYYY-MM-DD --frequency DAILY
  | analytics sales --type SALES --subtype SUMMARY --frequency DAILY --date YYYY-MM-DD
  | finance reports --report-type FINANCIAL --region ZZ --date YYYY-MM
  | ads campaigns list | ads reports preset --level campaigns --last-days 7 --fields campaignName,impressions,taps,localSpend
- Unsure about flags: asc_help(command="reviews list"); unsure which command: asc_help(command="search words")."""


def command_path(argv: list[str]) -> tuple[str, ...]:
    out = []
    for a in argv:
        if a.startswith("-"):
            break
        out.append(a)
    return tuple(out)


def _flag_name(token: str) -> Optional[str]:
    """`--file=x`, `-file` -> "file"; None for a token that is not a flag."""
    if not token.startswith("-") or token.lstrip("-") == "":
        return None
    return token.lstrip("-").split("=", 1)[0]


def _flag_value(argv: list[str], flag: str) -> Optional[str]:
    name = flag.lstrip("-")
    for i, a in enumerate(argv):
        if _flag_name(a) != name:
            continue
        if "=" in a:
            return a.split("=", 1)[1]
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


def _set_flag(argv: list[str], flag: str, value: str, at: Optional[int] = None) -> list[str]:
    """Replace any spelling of `flag`. `at` = index to insert at (right after the command path), else append."""
    out, skip = [], False
    for i, a in enumerate(argv):
        if skip:
            skip = False
            continue
        if _flag_name(a) == flag.lstrip("-"):
            skip = "=" not in a
            continue
        out.append(a)
    if at is None:
        return out + [flag, value]
    return out[:at] + [flag, value] + out[at:]


def _tail(text: str, n: int = 1200) -> str:
    text = text.strip()
    return text[-n:] if len(text) > n else text


def resolve_app(value: str, apps: list[dict]) -> list[dict]:
    """Exact id, bundle id or name (case-insensitive) first; then substring of the name or bundle id."""
    v = value.strip().lower()
    for key in ("id", "bundle_id", "name"):
        hits = [a for a in apps if str(a[key]).lower() == v]
        if hits:
            return hits
    return [a for a in apps if v in a["name"].lower() or v in a["bundle_id"].lower()]


def parse_orgs(text: str) -> list[dict]:
    try:
        data = json.loads(text)
    except ValueError:
        return []
    items = data if isinstance(data, list) else data.get("data", []) if isinstance(data, dict) else []
    out = []
    for it in items:
        if isinstance(it, dict):
            oid = it.get("orgId") or it.get("id")
            if oid is not None:
                out.append({"id": str(oid), "name": str(it.get("orgName") or it.get("name") or oid)})
    return out


async def _run(argv: list[str], env: dict, timeout: float, cwd: Path) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL, cwd=str(cwd),
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    finally:
        if proc.returncode is None:   # timeout or cancellation: never leave asc running
            proc.kill()
            await proc.wait()
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


def _run_sync(argv: list[str], env: dict, timeout: float, cwd: Path) -> tuple[int, str, str]:
    p = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=timeout, cwd=str(cwd))
    return p.returncode, p.stdout, p.stderr


class AscConnector:
    name = "asc"
    title = "App Store Connect + Apple Ads (asc)"

    def __init__(self, binary: Path, timeout: float = 180, preview_chars: int = 4000,
                 runner: Optional[Runner] = None, sync_runner: Optional[SyncRunner] = None,
                 cwd: Optional[Path] = None):
        self.binary, self.timeout, self.preview_chars = Path(binary), timeout, preview_chars
        # asc's working directory: Hermie-owned (data_dir/connectors), so a ./.asc/config.json planted in the
        # workspace by sandboxed code can never replace the keychain credentials
        self.cwd = Path(cwd) if cwd is not None else Path("~/.hermie/connectors").expanduser()
        self.runner = runner or _run
        self.sync_runner = sync_runner or _run_sync
        self._status: Optional[Status] = None

    # ------------------------------------------------------------ protocol
    def status(self) -> Status:
        if self._status is None:
            self._status = self._check_status()
        return self._status

    def _check_status(self) -> Status:
        if not self.binary.exists():
            return Status("missing", f"asc not found at {self.binary}; install it or set ASC_PATH")
        try:
            code, out, err = self._call_sync([str(self.binary), "auth", "status"], self._env({}), 15)
            data = json.loads(out)
        except Exception as e:
            return Status("error", f"`asc auth status` failed: {type(e).__name__}")
        if not isinstance(data, dict):
            return Status("error", "`asc auth status` returned unexpected output")
        if data.get("credentials") or data.get("environmentCredentialsComplete"):
            return Status("ready")
        return Status("not_authenticated", "run `asc auth login` in a terminal")

    def instructions(self) -> str:
        return ASC_INSTRUCTIONS

    def tools(self) -> list[ConnectorTool]:
        return [
            ConnectorTool("asc", "Read App Store Connect / Apple Ads data with the asc CLI (read-only).",
                          {"type": "object", "properties": {"args": {"type": "array", "items": {"type": "string"},
                                                                     "description": "asc arguments, e.g. [\"reviews\",\"list\",\"--app\",\"My App\"]"}},
                           "required": ["args"]}, self._asc),
            ConnectorTool("asc_help", "Show asc help for a command path, or search asc commands by words.",
                          {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
                          self._help),
        ]

    # ------------------------------------------------------------ process
    def _workdir(self) -> Path:
        self.cwd.mkdir(parents=True, exist_ok=True)
        return self.cwd

    async def _call(self, argv: list[str], env: dict, timeout: float) -> tuple[int, str, str]:
        return await self.runner(argv, env, timeout, self._workdir())

    def _call_sync(self, argv: list[str], env: dict, timeout: float) -> tuple[int, str, str]:
        return self.sync_runner(argv, env, timeout, self._workdir())

    # ------------------------------------------------------------ environment
    def _env(self, state: dict) -> dict:
        env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin",
               "HOME": str(Path.home()), "USER": os.environ.get("USER", ""),
               "TMPDIR": os.environ.get("TMPDIR", "/tmp"), "LANG": "en_US.UTF-8",
               "DO_NOT_TRACK": "1", "ASC_TELEMETRY_DISABLED": "1"}
        if state.get("vendor"):
            env["ASC_VENDOR_NUMBER"] = env["ASC_ANALYTICS_VENDOR_NUMBER"] = state["vendor"]
        if state.get("ads_org"):
            env["ASC_ADS_ORG_ID"] = state["ads_org"]
        return env

    # ------------------------------------------------------------ tools
    async def _asc(self, args: dict, ctx: ConnectorContext) -> ConnectorResult:
        argv = [str(a) for a in args.get("args") or []]
        path = command_path(argv)
        label = " ".join(path)
        if path not in ALLOWED:   # label "refused": the model's tokens never reach connectors.jsonl
            return ConnectorResult(f"Refused: `asc {label}` is not an allowed read-only command. Allowed: "
                                   + "; ".join(" ".join(p) for p in sorted(ALLOWED)), label="refused", ok=False)
        if "--" in argv:
            return ConnectorResult("A bare `--` is not allowed in asc arguments.", label=label, ok=False)
        verbs = sorted({a for a in argv[len(path):] if a.casefold() in WRITE_VERBS})
        if verbs:
            return ConnectorResult(f"Refused: `{', '.join(verbs)}` is a write verb; this connector is read-only.",
                                   label=label, ok=False)
        for i, a in enumerate(argv):
            if _flag_name(a) == "next":
                nxt = a.split("=", 1)[1] if "=" in a else (argv[i + 1] if i + 1 < len(argv) else "")
                if not nxt.startswith(NEXT_PREFIXES):
                    return ConnectorResult("--next is only accepted with an Apple API URL (links.next) from an "
                                           "earlier result.", label=label, ok=False)
        bad = sorted({"--" + n for n in map(_flag_name, argv) if n in REFUSED_FLAGS})
        if bad:
            return ConnectorResult(f"{', '.join(bad)} not allowed: Hermie saves the full result to the data room "
                                   "itself and returns its path.", label=label, ok=False)
        state = ctx.state()
        argv, problem = await self._resolve_app(path, argv, ctx, state)
        problem = problem or await self._fill_vendor(path, argv, ctx, state)
        problem = problem or await self._fill_org(path, argv, ctx, state)
        if problem:
            return ConnectorResult(problem, label=label, ok=False)
        # Hermie's own flags go right after the command path: Go flag parsing stops at the first stray positional, so
        # flags appended at the end could be silently turned into positionals
        own: list[str] = []
        if path == ("analytics", "request"):
            own += ["--reuse-existing"]
        out_file = None
        if path in FILE_COMMANDS:
            out_file = ctx.room_path("asc-" + "-".join(path) + FILE_COMMANDS[path])
            own += ["--output", str(out_file), "--decompress", "--output-format", "json"]
        else:
            own += ["--output", "json"]
        argv = argv[:len(path)] + own + argv[len(path):]
        try:
            code, out, err = await self._call([str(self.binary), *argv], self._env(state), self.timeout)
        except asyncio.TimeoutError:
            return ConnectorResult(f"asc {label} timed out after {self.timeout:.0f}s; try a narrower date range or "
                                   "fewer pages.", label=label, ok=False)
        except (OSError, ValueError) as e:
            return ConnectorResult(f"asc {label} could not run: {type(e).__name__}: {e}", label=label, ok=False)
        if code != 0:
            text = err + out
            hint = f"\n{ADS_HINT}" if "credentials" in text and (re.search(r"\bads:", text) or path[0] == "ads") else ""
            return ConnectorResult(f"asc {label} failed (exit {code}):\n{_tail(err or out)}{hint}", label=label, ok=False)
        if out_file is not None:
            found = self._find_output(out_file)
            if found is None:
                return ConnectorResult(f"asc {label} reported success but wrote no report file.\n{_tail(out)}",
                                       label=label, ok=False)
            return ConnectorResult(preview_table(found.read_text(errors="replace"), self.preview_chars), (found,), label)
        saved = ctx.room_path("asc-" + "-".join(path) + ".json")
        saved.write_text(out, encoding="utf-8")
        return ConnectorResult(preview_json(out, self.preview_chars), (saved,), label)

    @staticmethod
    def _find_output(out_file: Path) -> Optional[Path]:
        if out_file.exists():
            return out_file
        gz = out_file.with_name(out_file.name + ".gz")
        if gz.exists():
            with gzip.open(gz, "rb") as f:
                out_file.write_bytes(f.read())
            gz.unlink()
            return out_file
        return None

    async def _help(self, args: dict, ctx: ConnectorContext) -> ConnectorResult:
        words = str(args.get("command", "")).split()
        path = tuple(words)
        if path and any(p[:len(path)] == path for p in ALLOWED):
            argv = [str(self.binary), *path, "--help"]
        elif path and path[0] in {"ads", "apps", "reviews", "analytics", "insights", "finance"} and path not in ALLOWED:
            return ConnectorResult(f"`asc {' '.join(path)}` is not an allowed read-only command.", label="help", ok=False)
        else:
            argv = [str(self.binary), "search", " ".join(words), "--output", "json"]
        try:
            code, out, err = await self._call(argv, self._env({}), 30)
        except asyncio.TimeoutError:
            return ConnectorResult("asc help timed out.", label="help", ok=False)
        except (OSError, ValueError) as e:
            return ConnectorResult(f"asc help could not run: {type(e).__name__}: {e}", label="help", ok=False)
        return ConnectorResult(cap_text(out or err, self.preview_chars), label="help", ok=code == 0)

    # ------------------------------------------------------------ IDs the user never types
    async def apps(self, state: dict, refresh: bool = False) -> list[dict]:
        cat = state.get("catalog")
        if not refresh and cat and time.time() - cat.get("ts", 0) < CATALOG_TTL_S:
            return cat["apps"]
        code, out, err = await self._call([str(self.binary), "apps", "list", "--paginate", "--output", "json"],
                                           self._env(state), self.timeout)
        if code != 0:
            raise RuntimeError(f"asc apps list failed (exit {code}): {_tail(err or out, 300)}")
        rows = json.loads(out).get("data", [])
        apps = [{"id": str(r.get("id")), "name": str(r.get("attributes", {}).get("name", "")),
                 "bundle_id": str(r.get("attributes", {}).get("bundleId", ""))} for r in rows]
        state["catalog"] = {"ts": time.time(), "apps": apps}
        return apps

    async def _resolve_app(self, path: tuple, argv: list[str], ctx: ConnectorContext,
                           state: dict) -> tuple[list[str], Optional[str]]:
        flag = APP_FLAG.get(path, "--app")
        value = _flag_value(argv, flag)
        if value is None and path not in APP_REQUIRED:
            return argv, None
        try:
            apps = await self.apps(state)
        except Exception as e:
            return argv, f"Could not list your apps: {e}"
        if not apps:
            return argv, "No apps found in App Store Connect for this account."
        if value is not None:
            hits = resolve_app(value, apps)
            if len(hits) != 1:
                try:
                    apps = await self.apps(state, refresh=True)
                except Exception as e:
                    return argv, f"Could not list your apps: {e}"
                hits = resolve_app(value, apps)
            if len(hits) == 1:
                return _set_flag(argv, flag, hits[0]["id"], len(path)), None
            options = hits or apps
        else:
            default = ctx.session_state().get("app")
            if default and any(a["id"] == default for a in apps):
                return _set_flag(argv, flag, default, len(path)), None
            if len(apps) == 1:
                return _set_flag(argv, flag, apps[0]["id"], len(path)), None
            options = apps
        names = [a["name"] for a in options]
        choice = await ctx.choose("Which app?", names)
        if choice is None or choice not in names:
            return argv, ("No app selected. Your apps: " + ", ".join(a["name"] for a in apps)
                          + f". Name one of them with {flag}.")
        app = options[names.index(choice)]
        ctx.session_state()["app"] = app["id"]
        return _set_flag(argv, flag, app["id"], len(path)), None

    async def _fill_vendor(self, path: tuple, argv: list[str], ctx: ConnectorContext, state: dict) -> Optional[str]:
        needs = path in VENDOR_REQUIRED or (path == ("insights", "weekly") and _flag_value(argv, "--source") == "sales")
        if not needs or _flag_value(argv, "--vendor") or state.get("vendor"):
            return None
        answer = await ctx.ask("Your App Store Connect vendor number (App Store Connect > Payments and Financial "
                               "Reports, top left). Hermie asks only once.")
        answer = (answer or "").strip()
        if not answer.isdigit():
            return ("The vendor number is needed for sales and finance reports and was not given. Ask the user to "
                    "run the question again and enter it when asked (digits only).")
        state["vendor"] = answer
        return None

    async def _fill_org(self, path: tuple, argv: list[str], ctx: ConnectorContext, state: dict) -> Optional[str]:
        if path[0] != "ads" or path in ORGLESS or _flag_value(argv, "--org") or state.get("ads_org"):
            return None
        try:
            code, out, err = await self._call([str(self.binary), "ads", "acls", "list", "--output", "json"],
                                               self._env(state), self.timeout)
        except asyncio.TimeoutError:
            return "Apple Ads did not answer in time."
        except (OSError, ValueError) as e:
            return f"Apple Ads could not be reached: {type(e).__name__}: {e}"
        if code != 0:
            return f"Apple Ads is not reachable: {_tail(err or out, 300)}\n{ADS_HINT}"
        orgs = parse_orgs(out)
        if not orgs:
            return "No Apple Ads organization found for these credentials."
        if len(orgs) == 1:
            state["ads_org"] = orgs[0]["id"]
            return None
        names = [o["name"] for o in orgs]
        choice = await ctx.choose("Which Apple Ads organization?", names)
        if choice is None or choice not in names:
            return "No Apple Ads organization selected: " + ", ".join(names)
        state["ads_org"] = orgs[names.index(choice)]["id"]
        return None
