"""Command line: hermie serve | tail | show | allow | stats | forget | hide | status | hook | install-hooks."""
from __future__ import annotations

import argparse
import getpass
import json
import re
import shutil
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import uvicorn

from hermie import __version__
from hermie.config import Config
from hermie import claude_code
from hermie.gate.redact import PLACEHOLDER, MappingStore, MappingStoreError
from hermie.proxy.approvals import AllowStore, NoPrompter, TtyPrompter
from hermie.proxy.receipt import BodyStore, Receipt, ReceiptLine

_SINCE = re.compile(r"(\d+)([mhd])")


def _since(raw: str) -> datetime:
    m = _SINCE.fullmatch(raw.strip())
    if not m:
        raise ValueError(f"invalid --since {raw!r} (use 10m, 2h or 1d)")
    unit = {"m": "minutes", "h": "hours", "d": "days"}[m.group(2)]
    return datetime.now(timezone.utc) - timedelta(**{unit: int(m.group(1))})


def _local_time(at: str) -> str:
    try:
        when = datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return when.astimezone().strftime("%H:%M:%S")
    except ValueError:
        return at[11:19]


def _format_line(line: ReceiptLine) -> str:
    total = sum(int(p.get("size") or 0) for p in line.new_parts)
    out = [f"{_local_time(line.at)}  {line.client} -> {line.upstream}/{line.model or '-'}  "
           f"{'stream' if line.stream else 'json'}  +{len(line.new_parts)} parts, {total} bytes new"]
    for p in line.new_parts:
        ents = ", ".join(p.get("entities") or []) or "0 findings"
        out.append(f"  [{p.get('origin')}]  {p.get('tool') or '-'}  {p.get('size')}  {ents}")
    for wid in line.withheld:
        out.append(f"  ⏸ withheld {wid}  hermie allow {wid}")
    if line.held:
        out.append(f"  ⏸ held {line.held} ({line.approved_by or 'rejected'})  hermie allow {line.held}")
    if line.upstream_error:
        out.append(f"  ! upstream {line.upstream_error}")
    return "\n".join(out)


def _serve(config: Config, args) -> int:
    from hermie.proxy.server import create_app

    tty = sys.stdin is not None and sys.stdin.isatty()
    prompter = TtyPrompter(config.hold_timeout_s) if tty else NoPrompter()
    app = create_app(config, prompter=prompter)
    base = f"http://{config.host}:{config.port}"
    home = str(Path.home())
    data = str(config.data_dir).replace(home, "~", 1)
    print(f"hermie {__version__}  mode={config.mode}  judge={config.judge or 'off'}  "
          f"bodies={'on' if config.bodies else 'off'}  data={data}")
    print("Point your client at one of these:")
    print(f"  ANTHROPIC_BASE_URL={base}/anthropic      (Claude Code, aider)")
    print(f"  OPENAI_BASE_URL={base}/openai/v1         (aider, Codex CLI: base_url in config.toml)")
    print(f"  GOOGLE_GEMINI_BASE_URL={base}/gemini     (Gemini CLI)")
    print(f"  custom upstream: {base}/custom -> {config.custom_upstream or 'off'}")
    print("TTY: prompts enabled (held messages ask here)" if tty else
          "no TTY: held messages are rejected, use `hermie allow ID`")
    sys.stdout.flush()
    claude_code.write_serve_file(config)
    try:
        uvicorn.run(app, host=config.host, port=config.port, log_level="warning")
    finally:
        claude_code.remove_serve_file(config)
    return 0


def _tail(config: Config, args) -> int:
    receipt = Receipt(config)
    since = _since(args.since) if args.since else None
    if args.once:
        lines = receipt.iter(since=since)
    else:
        floor = since.strftime("%Y-%m-%dT%H:%M:%SZ") if since else None
        lines = (ln for ln in receipt.follow() if floor is None or ln.at >= floor)
    try:
        for line in lines:
            if args.json:
                print(json.dumps(line.__dict__, ensure_ascii=False), flush=True)
            else:
                print(_format_line(line), flush=True)
    except KeyboardInterrupt:
        return 0
    return 0


def _show(config: Config, args) -> int:
    raw = BodyStore(config).get(args.request_id)
    if raw is None:
        print(f"hermie: no stored body for {args.request_id}", file=sys.stderr)
        return 1
    try:
        text = json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
    except ValueError:
        text = raw.decode("utf-8", errors="replace")
    tty = sys.stdout.isatty()
    wrap = (lambda m: f"\x1b[1m{m.group(0)}\x1b[0m") if tty else (lambda m: f"«{m.group(0)}»")
    print(PLACEHOLDER.sub(wrap, text))
    return 0


def _allow(config: Config, args) -> int:
    store = AllowStore(config.data_dir)
    if not store.allow(args.id):
        item = store.pending.get(args.id)
        if item is not None:
            print(f"hermie: {args.id} could not be scanned and cannot be released", file=sys.stderr)
        else:
            print("hermie: unknown id", file=sys.stderr)
        return 1
    print(f"allowed {args.id}")
    return 0


def _stats(config: Config, args) -> int:
    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    clients: Counter = Counter()
    replaced: Counter = Counter()
    withheld = held = approved = unrestored = errors = 0
    for ln in Receipt(config).iter(since=since):
        clients[ln.client] += 1
        replaced.update({k: int(v) for k, v in ln.replaced.items()})
        withheld += len(ln.withheld)
        held += 1 if ln.held else 0
        approved += 1 if ln.approved_by is not None else 0
        unrestored += ln.unrestored or 0
        errors += 1 if ln.upstream_error else 0
    print(f"last {args.days} day(s)")
    print(f"requests          {sum(clients.values())}")
    for name, n in sorted(clients.items()):
        print(f"  {name:<16}{n}")
    print("replaced")
    for name, n in sorted(replaced.items()):
        print(f"  {name:<16}{n}")
    if not replaced:
        print("  none")
    print(f"withheld          {withheld}")
    print(f"held              {held}")
    print(f"approved          {approved}")
    print(f"unrestored        {unrestored}")
    print(f"upstream errors   {errors}")
    return 0


def _forget(config: Config, args) -> int:
    print("This deletes the placeholder mapping. Placeholders in old conversations will no longer\n"
          "restore to the original values. Placeholder numbers are never reused, so an old placeholder\n"
          "can never restore to a different value. A running `hermie serve` notices on its next request:\n"
          "it drops its cache, and values that reappear get new placeholders.")
    if not args.yes:
        try:
            answer = input("Clear the mapping? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer != "y":
            print("kept")
            return 0
    MappingStore(config.mapping_path).clear()
    print("mapping cleared")
    return 0


_ENTITY = re.compile(r"[A-Z][A-Z_]*")
MIN_HIDE_LEN = 4   # the known-values pass matches literally from four characters


def _hide(config: Config, args) -> int:
    """Put values into the mapping by hand, so they are replaced wherever they appear from now on."""
    if not _ENTITY.fullmatch(args.entity):
        print("hermie: --as must be upper-case letters and underscores, e.g. SECRET or TOKEN", file=sys.stderr)
        return 2
    values = list(args.value)
    if not values:
        if sys.stdin is not None and sys.stdin.isatty():
            values = [getpass.getpass("value to hide (not echoed): ")]
        else:
            values = [ln.rstrip("\r\n") for ln in sys.stdin]
    values = [v for v in values if v.strip()]
    if not values:
        print("hermie: nothing to hide", file=sys.stderr)
        return 2
    if any(len(v) < MIN_HIDE_LEN for v in values):
        print(f"hermie: a value must be at least {MIN_HIDE_LEN} characters", file=sys.stderr)
        return 2
    names: list[str] = []

    def mint(mapping: dict, counters: dict) -> dict:
        names.clear()
        by_value = {v: k for k, v in mapping.items()}
        new: dict[str, str] = {}
        for v in values:
            if v in by_value:
                names.append(by_value[v])
                continue
            counters[args.entity] = counters.get(args.entity, 0) + 1
            ph = f"<{args.entity}_{counters[args.entity]}>"
            new[ph] = v
            by_value[v] = ph
            names.append(ph)
        return new

    try:
        MappingStore(config.mapping_path).update(mint)
    except MappingStoreError as e:
        print(f"hermie: {e}", file=sys.stderr)
        return 1
    for ph in names:
        print(ph)
    return 0


def _status(config: Config, args) -> int:
    if args.json:
        print(json.dumps(claude_code.status_json(config), ensure_ascii=False))
    else:
        print(claude_code.status_text(config))
    return 0


def _hook(config: Config, args) -> int:
    """Claude Code hook entry point: one JSON event on stdin, an optional JSON reply on stdout, always exit 0."""
    try:
        event = json.loads(sys.stdin.read())
    except (ValueError, OSError):
        return 0
    out = claude_code.handle_hook(event, config)
    if out:
        print(json.dumps(out, ensure_ascii=False))
    return 0


def _hermie_command() -> str:
    found = shutil.which("hermie")
    return found if found else f"{sys.executable} -m hermie.cli"


def _install_hooks(config: Config, args) -> int:
    path = (Path.home() / ".claude" / "settings.json") if args.user else Path.cwd() / ".claude" / "settings.local.json"
    try:
        if args.uninstall:
            claude_code.uninstall(path)
            print(f"removed Hermie's hooks and status line from {path}")
            return 0
        notes = claude_code.install(path, _hermie_command(), args.data_dir)
    except (OSError, ValueError) as e:
        print(f"hermie: {e}", file=sys.stderr)
        return 1
    print(f"wrote {path}")
    print("  hooks: UserPromptSubmit, PostToolUse (file writes), Stop -> `hermie hook`")
    for n in notes:
        print(f"  {n}")
    if not notes:
        print("  status line -> `hermie status`")
    print("Takes effect in a new Claude Code session. Start it with ANTHROPIC_BASE_URL pointed at `hermie serve`.")
    return 0


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hermie", description="Local privacy proxy for coding agents.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--data-dir", help="data directory (default ~/.hermie)")
        sp.add_argument("--config", help="path to config.toml")
        sp.set_defaults(fn=fn)
        return sp

    sp = add("serve", _serve, "run the proxy")
    sp.add_argument("--host")
    sp.add_argument("--port", type=int)
    sp.add_argument("--mode", choices=["enforce", "observe"])
    sp.add_argument("--judge", help="ollama:MODEL or laya:PATH (a fine-tuned Laya checkpoint, needs laya-mlx)")
    sp.add_argument("--upstream", help="custom upstream URL")
    sp.add_argument("--no-bodies", action="store_true", help="do not store outbound bodies")
    sp = add("tail", _tail, "follow the receipt log")
    sp.add_argument("--since", help="10m, 2h, 1d")
    sp.add_argument("--once", action="store_true", help="print existing lines and exit")
    sp.add_argument("--json", action="store_true", help="raw receipt lines")
    sp = add("show", _show, "print the stored outbound body of a request")
    sp.add_argument("request_id")
    sp = add("allow", _allow, "allow a withheld or held item")
    sp.add_argument("id")
    sp = add("stats", _stats, "summarize the receipt log")
    sp.add_argument("--days", type=float, default=1)
    sp = add("forget", _forget, "clear the placeholder mapping")
    sp.add_argument("--yes", action="store_true", help="do not ask")
    sp = add("hide", _hide, "add values to the mapping by hand (replaced wherever they appear from now on)")
    sp.add_argument("value", nargs="*", help="values to hide; none: read from stdin (a TTY prompts without echo)")
    sp.add_argument("--as", dest="entity", default="SECRET", help="placeholder entity (default SECRET)")
    sp = add("status", _status, "one line for a status bar: proxy up, values kept local, last request")
    sp.add_argument("--json", action="store_true")
    add("hook", _hook, "Claude Code hook entry point (reads the event JSON from stdin)")
    sp = add("install-hooks", _install_hooks, "add Hermie's hooks and status line to Claude Code's settings")
    sp.add_argument("--user", action="store_true", help="~/.claude/settings.json instead of ./.claude/settings.local.json")
    sp.add_argument("--uninstall", action="store_true", help="remove them again")
    return p


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    overrides: dict = {}
    if args.data_dir:
        overrides["data_dir"] = Path(args.data_dir)
    if args.cmd == "serve":
        for key in ("host", "port", "mode", "judge"):
            if getattr(args, key) is not None:
                overrides[key] = getattr(args, key)
        if args.upstream:
            overrides["custom_upstream"] = args.upstream
        if args.no_bodies:
            overrides["bodies"] = False
    try:
        config = Config.load(Path(args.config) if args.config else None, **overrides)
        if args.cmd == "tail" and args.since:
            _since(args.since)
    except (ValueError, OSError) as e:
        print(f"hermie: {e}", file=sys.stderr)
        return 2
    return args.fn(config, args)


if __name__ == "__main__":
    sys.exit(main())
