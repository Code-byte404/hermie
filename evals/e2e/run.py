"""Run the end-to-end tasks through Claude Code, once through Hermie and once directly, recording what leaves.

    python -m evals.e2e.run --out DIR [--seed 1] [--arms hermie,direct] [--tasks ID,...] [--rep 1|2,3] [--claude claude]

Per task, arm and repeat: a fresh copy of the workspace (without canaries.json) as cwd, a fresh recorder in front of
the upstream, and for the Hermie arm a fresh Hermie process and data dir (enforce, judge off, `--languages`, no
prompter: a held tool result is withheld, a held user message is a 422). Then `claude -p` (follow-ups with
`--resume`). Results under DIR/runs/<arm>/<task>/rep<N>/: recorded/, workdir/, session.json, answer.txt, status
(ok, blocked, error, timeout) and, for the Hermie arm, hermie_data/. A run dir with a status is skipped, so the run
can be resumed. The workspace is built once (DIR/workspace); later invocations keep its canaries frozen.

This reaches the real Anthropic API through the user's Claude Code login. Start it only with the user's go-ahead.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from evals.creddata.run_hermie import _git_commit
from evals.e2e.recorder import serve_recorder
from evals.e2e.tasks import TASKS, Task
from evals.e2e.workspace import build

ARMS = ("hermie", "direct")
CLAUDE_TIMEOUT_S = 600
# no user settings (hooks, plugins, model), no MCP servers, no web tools: every byte the model sends goes through the
# API, so through the recorder. --disallowedTools takes a list, so it stays last.
CLAUDE_FLAGS = ["--setting-sources", "project,local", "--strict-mcp-config", "--mcp-config", '{"mcpServers": {}}',
                "--disallowedTools", "WebFetch", "WebSearch"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def child_env() -> dict[str, str]:
    """The parent's environment without HERMIE_* (the user's overrides) and Claude Code's own session markers."""
    return {k: v for k, v in os.environ.items()
            if not k.startswith(("HERMIE_", "CLAUDE_CODE_")) and k != "CLAUDECODE"}


def hermie_config(port: int, data_dir: Path, upstream: str, languages: str):
    """Built directly, not with Config.load: ~/.hermie/config.toml and HERMIE_* must not change the run."""
    from hermie.config import Config
    return Config(port=port, data_dir=data_dir, mode="enforce", judge=None, languages=tuple(languages.split(",")),
                  anthropic_upstream=upstream)


def serve_hermie(argv: list[str]) -> int:
    """Hermie's app with no prompter (a headless `hermie serve` on macOS would open a dialog for a held item)."""
    import uvicorn

    from hermie.proxy.approvals import NoPrompter
    from hermie.proxy.server import create_app
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--data-dir", type=Path, required=True)
    ap.add_argument("--upstream", required=True)
    ap.add_argument("--languages", default="en,zh")
    a = ap.parse_args(argv)
    config = hermie_config(a.port, a.data_dir, a.upstream, a.languages)
    uvicorn.run(create_app(config, prompter=NoPrompter()), host="127.0.0.1", port=a.port, log_level="warning")
    return 0


def _wait_port(port: int, proc: subprocess.Popen, timeout: float = 180) -> None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if proc.poll() is not None:
            raise RuntimeError(f"hermie exited with {proc.returncode}")
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"hermie did not listen on {port}")


def classify(returncode: int | None, outputs: list[dict], receipt: Path, timed_out: bool) -> str:
    if timed_out:
        return "timeout"
    if receipt.is_file():
        for line in receipt.read_text().splitlines():
            if line.strip() and json.loads(line).get("status") == 422:
                return "blocked"
    if returncode != 0 or not outputs or any(o.get("is_error") for o in outputs):
        return "error"
    return "ok"


def _claude_env(base_url: str) -> dict[str, str]:
    """Any request not to 127.0.0.1 (the base URL) goes to a proxy port nobody listens on: an exit around the
    recorder fails loudly instead of leaving unrecorded."""
    env = child_env()
    dead = f"http://127.0.0.1:{_free_port()}"
    env.update(ANTHROPIC_BASE_URL=base_url, HTTPS_PROXY=dead, HTTP_PROXY=dead, https_proxy=dead, http_proxy=dead,
               NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
    return env


def _parse(stdout: str) -> dict:
    for line in reversed(stdout.strip().splitlines()):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return {"is_error": True, "result": stdout[-2000:]}


def run_one(task: Task, arm: str, rep: int, out: Path, a) -> tuple[str, int, str | None] | None:
    """(status, sessions used, model) or None when the run already exists."""
    rd = out / "runs" / arm / task.id / f"rep{rep}"
    if (rd / "status").is_file():
        return None
    if rd.exists():
        shutil.rmtree(rd)
    rd.mkdir(parents=True)
    # the model works in a temp folder away from the run files (mapping, canaries); copied back for grading
    workdir = Path(tempfile.mkdtemp(prefix="hermie-e2e-")) / "work"
    shutil.copytree(out / "workspace", workdir, ignore=shutil.ignore_patterns("canaries.json"))
    outputs, sessions, returncode, timed_out, proc = [], 0, None, False, None
    rport = _free_port()
    with serve_recorder(a.recorder_upstream, rd / "recorded", rport):
        try:
            if arm == "hermie":
                hport = _free_port()
                log = open(rd / "hermie.log", "w")
                proc = subprocess.Popen([sys.executable, "-m", "evals.e2e.run", "serve-hermie", "--port", str(hport),
                                         "--data-dir", str(rd / "hermie_data"), "--upstream",
                                         f"http://127.0.0.1:{rport}", "--languages", a.languages],
                                        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                        env=child_env())
                _wait_port(hport, proc)
                base = f"http://127.0.0.1:{hport}/anthropic"
            else:
                base = f"http://127.0.0.1:{rport}"
            sid = None
            for prompt in (task.prompt, *task.followups):
                cmd = [a.claude, "-p", prompt, "--output-format", "json", "--permission-mode", "bypassPermissions"]
                if sid:
                    cmd += ["--resume", sid]
                cmd += CLAUDE_FLAGS
                sessions += 1
                try:
                    r = subprocess.run(cmd, cwd=workdir, env=_claude_env(base), capture_output=True, text=True,
                                       timeout=CLAUDE_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    break
                returncode = r.returncode
                outputs.append(_parse(r.stdout))
                sid = outputs[-1].get("session_id")
                if r.returncode != 0 or outputs[-1].get("is_error") or not sid:
                    break
        finally:
            if proc is not None:
                proc.terminate()
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    proc.kill()
            shutil.copytree(workdir, rd / "workdir")
            shutil.rmtree(workdir.parent, ignore_errors=True)
    status = classify(returncode, outputs, rd / "hermie_data" / "receipt.jsonl", timed_out)
    (rd / "session.json").write_text(json.dumps(outputs, ensure_ascii=False, indent=1))
    (rd / "answer.txt").write_text("\n\n---\n\n".join(str(o.get("result", "")) for o in outputs))
    (rd / "status").write_text(status)
    model = next((m for o in outputs for m in (o.get("modelUsage") or {})), None)
    return status, sessions, model


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["serve-hermie"]:
        return serve_hermie(argv[1:])
    ap = argparse.ArgumentParser(prog="python -m evals.e2e.run")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--arms", default=",".join(ARMS))
    ap.add_argument("--tasks", default="")
    ap.add_argument("--rep", default="1")
    ap.add_argument("--claude", default="claude")
    ap.add_argument("--languages", default="en,zh")
    ap.add_argument("--recorder-upstream", default="https://api.anthropic.com", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    out = a.out.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    mpath = out / "manifest.json"
    manifest = json.loads(mpath.read_text()) if mpath.is_file() else {}
    if (out / "workspace" / "canaries.json").is_file():
        if manifest.get("seed") not in (None, a.seed):
            print(f"workspace exists: canaries frozen at seed {manifest['seed']} (--seed {a.seed} ignored)")
    else:
        if (out / "workspace").exists():
            shutil.rmtree(out / "workspace")
        build(a.seed, out / "workspace")
        manifest["seed"] = a.seed
    v = subprocess.run([a.claude, "--version"], capture_output=True, text=True)
    manifest.update(hermie_commit=_git_commit(), claude_version=v.stdout.strip(),
                    hermie_config={"mode": "enforce", "judge": None, "languages": a.languages.split(","),
                                   "prompter": "none", "built": "directly (no config.toml, no HERMIE_*)"},
                    claude_flags=" ".join(CLAUDE_FLAGS))
    manifest.setdefault("sessions", 0)
    wanted = [t for t in a.tasks.split(",") if t]
    tasks = [t for t in TASKS if not wanted or t.id in wanted]
    unknown = set(wanted) - {t.id for t in tasks}
    if unknown:
        raise SystemExit(f"unknown tasks: {sorted(unknown)}")
    for task in tasks:
        for rep in [int(x) for x in a.rep.split(",")]:
            for arm in a.arms.split(","):
                got = run_one(task, arm, rep, out, a)
                if got is None:
                    continue
                status, sessions, model = got
                manifest["sessions"] += sessions
                if model:
                    manifest["model"] = model
                mpath.write_text(json.dumps(manifest, indent=1))
                print(f"{task.id:<22} {arm:<7} rep{rep}  {status}", flush=True)
    mpath.write_text(json.dumps(manifest, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
