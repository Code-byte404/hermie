"""Command line: hermie serve | tail | show | allow | stats | forget."""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import uvicorn

from hermie import __version__
from hermie.config import Config
from hermie.gate.redact import PLACEHOLDER, MappingStore
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
    uvicorn.run(app, host=config.host, port=config.port, log_level="warning")
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
    AllowStore(config.data_dir).allow(args.id)
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
          "restore to the original values. A running `hermie serve` keeps its in-memory copy until\n"
          "it is restarted.")
    if not args.yes and input("Clear the mapping? [y/N] ").strip().lower() != "y":
        print("kept")
        return 0
    MappingStore(config.mapping_path).clear()
    print("mapping cleared")
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
    sp.add_argument("--judge", help="e.g. ollama:MODEL")
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
