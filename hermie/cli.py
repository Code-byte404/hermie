"""Command-line entry point.

    hermie                          # full-screen UI (default mode)
    hermie --auto                   # full-screen UI, auto mode (skips approvals, boundaries unchanged)
    hermie --json "TASK" [MATERIAL]  # headless mode: prints a JSON event stream and the final result
    hermie --dangerously-no-sandbox # disables the sandbox (executor can access the network), warns at startup
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from .attachments import load_material
from .config import RunMode, Settings
from .events import Approval
from .policy import Force


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hermie", description="Local-first hybrid agent")
    p.add_argument("task", nargs="?", help="task description (used with --json)")
    p.add_argument("material", nargs="?", help="path to a material file or directory (optional; a directory attaches its file tree)")
    p.add_argument("--json", action="store_true", help="headless mode, prints JSON")
    p.add_argument("--auto", "--skip-permissions", dest="auto", action="store_true",
                   help="auto mode: skip manual approvals (sandbox, privacy gate, snapshots and audit still apply)")
    p.add_argument("--dangerously-no-sandbox", dest="no_sandbox", action="store_true",
                   help="DANGEROUS: disables the Seatbelt sandbox; the executor can access the network and the whole disk")
    p.add_argument("--workspace", type=Path, help="workspace directory (default: current directory)")
    p.add_argument("--force", choices=["local", "cloud"], help="force local or cloud")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--graph", action="store_true", help="print the task graph and step graph as Mermaid and exit")
    p.add_argument("--calibrate", action="store_true",
                   help="propose routing thresholds from your recorded tasks (trajectories.jsonl) and exit")
    p.add_argument("--apply", action="store_true", help="with --calibrate: write the proposal to .env")
    p.add_argument("--since", type=float, metavar="DAYS", help="with --calibrate: only tasks from the last DAYS days")
    return p


_TCC_DIRS = ("Desktop", "Documents", "Downloads")


def read_material(path: Path, s: Settings) -> str:
    """The material argument of --json: a text file's content or a directory's file tree, in the same format the
    UI uses for dropped files (read here in the main process; it goes through the privacy gate with the task)."""
    if not path.exists():
        raise SystemExit(f"✖ Material not found: {path}")
    m = load_material([path], max_file_chars=s.attach_max_file_chars, max_total_chars=s.attach_max_total_chars,
                      deny_names=s.sandbox_deny_names)
    for n in m.notes:
        print(f"⚠ Attachment: {n}", file=sys.stderr)
    return m.text


def resolve_workspace(arg: Path | None) -> tuple[Path, list[str]]:
    """Workspace = --workspace or the current directory. The home directory and the root are refused (the sandbox
    would open the whole directory to the executor for writing); directories under Desktop/Documents/Downloads get
    a TCC hint."""
    ws = (arg or Path.cwd()).expanduser().resolve()
    home = Path.home().resolve()
    if ws in (home, Path("/")) or ws == home.parent:
        raise SystemExit(f"✖ The workspace cannot be {ws}: the sandbox would open the whole directory to the executor for writing. cd into the project directory before starting, or pass --workspace.")
    warnings = []
    try:
        top = ws.relative_to(home).parts[0]
        if top in _TCC_DIRS:
            warnings.append(f"The workspace is under ~/{top}; macOS may show a privacy permission prompt. Consider keeping the project outside protected directories.")
    except ValueError:
        pass
    return ws, warnings


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    if args.graph:  # no models, no sandbox profile: the builders only keep a reference to the agent
        from types import SimpleNamespace

        from .graph import build_step_graph, build_task_graph, render
        shell = SimpleNamespace()
        shell.step_graph, shell.task_graph = build_step_graph(shell), build_task_graph(shell)
        print(render(shell))
        return
    if args.calibrate:
        from . import calibrate
        from .config import PROJECT_ROOT
        s = Settings()
        report = calibrate.build_report(s, since_days=args.since, eval_signals=PROJECT_ROOT / "evals" / "signals.jsonl")
        print(calibrate.format_report(report))
        if args.apply:
            written = calibrate.apply(s, report)
            print(f"\nWrote {', '.join(f'{k}={v}' for k, v in written.items())} to {s.env_path}" if written
                  else "\nNothing written.")
        return
    s = Settings()
    s.workspace, ws_warnings = resolve_workspace(args.workspace)
    s.ensure_dirs()
    for w in ws_warnings:
        print(f"⚠ {w}", file=sys.stderr)
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    level = logging.INFO if args.verbose else logging.WARNING
    if args.json:
        logging.basicConfig(level=level, format=fmt, stream=sys.stderr)
    else:  # full-screen UI: anything written to stderr corrupts the display, so logs always go to a file
        logging.basicConfig(level=level, format=fmt, filename=s.data_dir / "hermie.log", encoding="utf-8")
    if args.no_sandbox:
        s.mode = RunMode.NO_SANDBOX
        print("⚠️  No-sandbox mode: the executor can access the network and read/write the whole disk. The privacy gate is still on.", file=sys.stderr)
    elif args.auto:
        s.mode = RunMode.AUTO

    if args.json:
        if not args.task:
            _parser().error("--json requires a task description")
        material = read_material(Path(args.material).expanduser(), s) if args.material else ""
        asyncio.run(_headless(s, args.task, material, Force(args.force) if args.force else Force.NONE))
        return

    from .core import Hermie
    from .tui.app import HermieApp
    from .voice import prime_tqdm_lock

    # Warm the models up in the main thread before the UI starts: the first spaCy and torch load takes tens of
    # seconds and must not happen in a UI thread
    print("Loading privacy detection models and RouteLLM weights (about 1 minute the first time)...", file=sys.stderr, flush=True)
    agent = Hermie(s)
    agent.warm_up()
    prime_tqdm_lock()  # build tqdm's lock (used by mlx-whisper) before the UI takes over stderr; see voice.prime_tqdm_lock
    HermieApp(s, agent=agent).run()


async def _headless(s: Settings, task: str, material: str, force: Force) -> None:
    from .core import Hermie

    agent = Hermie(s)

    def emit(ev):
        d = ev.to_dict()
        if d["event"] == "StatsUpdated" or (d["event"] == "ChatMessage" and d.get("streaming")):
            return  # headless mode prints no streaming fragments or stats heartbeats; stats are in the final Result
        print(json.dumps(d, ensure_ascii=False, default=str), flush=True)

    agent.bus.subscribe(emit)

    async def approver(req):  # nobody to ask when headless: in default mode every high-risk action is denied
        print(json.dumps({"event": "ApprovalDenied", "command": req.summary, "reason": "cannot approve in headless mode"},
                         ensure_ascii=False), flush=True)
        return Approval.DENY

    agent.bus.approver = approver
    result = await agent.run(task, material, force)
    print(json.dumps({"event": "Result", **result.to_dict(), "stats": agent.session.stats.snapshot()},
                     ensure_ascii=False, default=str), flush=True)


if __name__ == "__main__":
    main()
