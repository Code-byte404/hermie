"""`hermie claude`: one command instead of four. It makes sure a proxy runs (starting `hermie serve` in the
background when none does), then opens Claude Code through it with the menu (`--plugin-dir`), the status line and
the hooks (`--settings`, this session only). A proxy it started stops when the last `hermie claude` window closes;
a proxy you started yourself is never stopped.

Bookkeeping lives under `<data_dir>/launch/`: one file per open window (named after its pid), `owner` (the pid of a proxy a
window started) and `lock` (held while a window starts or stops the proxy, so two windows never race)."""
from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from hermie import claude_code
from hermie.config import Config

MENU_DIR = Path(__file__).resolve().parent / "claude_menu"
SERVE_WAIT_S = 120.0   # the first start loads the language model (and the judge, when set)
STOP_WAIT_S = 5.0
LOG_TAIL = 15


def hermie_argv() -> list[str]:
    """This hermie, whatever is on PATH: an env's `hermie` script can be stale or broken, its python is not."""
    return [sys.executable, "-m", "hermie.cli"]


def serve_command(config_path: str | None = None, data_dir: str | None = None) -> list[str]:
    argv = hermie_argv() + ["serve"]
    if config_path:
        argv += ["--config", config_path]
    if data_dir:
        argv += ["--data-dir", data_dir]
    return argv


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@contextmanager
def _locked(dir: Path):
    dir.mkdir(parents=True, exist_ok=True)
    with open(dir / "lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _windows(dir: Path) -> list[int]:
    """Pids of open `hermie claude` windows; files of windows that died are removed."""
    live = []
    for p in dir.glob("window-*"):
        try:
            pid = int(p.name.split("-")[1])
        except ValueError:
            continue
        if _alive(pid):
            live.append(pid)
        else:
            p.unlink(missing_ok=True)
    return live


def _owner(dir: Path) -> int | None:
    try:
        return int((dir / "owner").read_text().strip())
    except (OSError, ValueError):
        return None


def _tail(path: Path, n: int = LOG_TAIL) -> str:
    try:
        return "\n".join(path.read_text("utf-8", errors="replace").splitlines()[-n:])
    except OSError:
        return ""


def _start(config: Config, serve_cmd: list[str], out, wait_s: float) -> dict | None:
    log = config.data_dir / "serve.log"
    print("Starting Hermie (the first start loads the language model, this takes a few seconds)...", file=out,
          flush=True)
    with open(log, "a") as f:
        proc = subprocess.Popen(serve_cmd, stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT,
                                start_new_session=True)   # Ctrl-C in Claude Code does not reach it
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        info = claude_code.serving(config)
        if info is not None and info["pid"] == proc.pid:
            return info
        if proc.poll() is not None:
            break
        time.sleep(0.2)
    else:
        proc.terminate()
    print(f"hermie: the proxy did not start, so Claude Code was not opened. The end of {log}:", file=out)
    print(_tail(log), file=out, flush=True)
    return None


def _stop(config: Config, pid: int) -> None:
    info = claude_code.serving(config)
    if info is None or info["pid"] != pid:
        return   # gone already, or the port now belongs to a proxy someone else started
    try:
        os.kill(pid, signal.SIGINT)   # uvicorn re-raises SIGTERM and dies before serve.json is removed
    except OSError:
        return
    deadline = time.monotonic() + STOP_WAIT_S
    while time.monotonic() < deadline:
        try:
            if os.waitpid(pid, os.WNOHANG)[0] == pid:
                return   # our own child, now reaped
        except ChildProcessError:
            pass   # started by another window
        if not _alive(pid):
            return
        time.sleep(0.1)


def _shim(config: Config) -> Path:
    """`<data_dir>/bin/hermie`, put first on Claude Code's PATH: the menu runs `hermie asks` and `hermie answer`,
    and this makes them reach this hermie. The directory holds nothing else, so Claude Code's other tools see the
    same programs as before."""
    bin = config.data_dir / "bin"
    bin.mkdir(parents=True, exist_ok=True)
    path = bin / "hermie"
    body = f'#!/bin/sh\nexec {json.dumps(sys.executable)} -m hermie.cli "$@"\n'
    try:
        if path.read_text() == body:
            return bin
    except OSError:
        pass
    tmp = bin / ".hermie.tmp"
    tmp.write_text(body)
    tmp.chmod(0o755)
    os.replace(tmp, path)
    return bin


def _default_settings_paths() -> list[Path]:
    return [Path.home() / ".claude" / "settings.json", Path.cwd() / ".claude" / "settings.json",
            Path.cwd() / ".claude" / "settings.local.json"]


def claude_command(claude: str, info: dict, config: Config, args: list[str], data_dir_given: bool,
                   settings_paths: list[Path]) -> tuple[list[str], dict]:
    data_dir = str(config.data_dir) if data_dir_given else None
    argv = [claude, "--plugin-dir", str(MENU_DIR)]
    if not any(claude_code.has_hooks(p) for p in settings_paths):
        argv += ["--settings", json.dumps(claude_code.hook_settings(hermie_argv(), data_dir))]
    env = dict(os.environ)
    env["ANTHROPIC_BASE_URL"] = f"http://{info['host']}:{info['port']}/anthropic"
    env["PATH"] = os.pathsep.join([str(_shim(config)), env.get("PATH", "")])
    if data_dir:
        env["HERMIE_DATA_DIR"] = data_dir
    return argv + list(args), env


def _call(argv: list[str], env: dict) -> int:
    proc = subprocess.Popen(argv, env=env)
    while True:
        try:
            return proc.wait()
        except KeyboardInterrupt:
            continue   # Claude Code handles Ctrl-C itself; this window waits for it to exit


def run(config: Config, args: list[str], *, serve_cmd: list[str], call=_call, which=None, out=None,
        data_dir_given: bool = False, settings_paths: list[Path] | None = None,
        wait_s: float = SERVE_WAIT_S) -> int:
    import shutil
    out = out or sys.stderr
    which = which or shutil.which
    settings_paths = _default_settings_paths() if settings_paths is None else settings_paths
    claude = which("claude")
    if claude is None:
        print("hermie: `claude` was not found on PATH. Install Claude Code first: "
              "https://docs.claude.com/en/docs/claude-code", file=out)
        return 127
    if config.judge is None:
        print("Hermie: judge off, only the pattern rules check your messages. To turn it on, add "
              'judge = "ollama:MODEL" to ~/.hermie/config.toml.', file=out)
    dir = config.data_dir / "launch"
    me = dir / f"window-{os.getpid()}-{os.urandom(4).hex()}"
    with _locked(dir):
        info = claude_code.serving(config)
        if info is None:
            (dir / "owner").unlink(missing_ok=True)
            info = _start(config, serve_cmd, out, wait_s)
            if info is None:
                return 1
            (dir / "owner").write_text(str(info["pid"]))
        me.write_text("")

    def hang_up(signum, frame):
        raise SystemExit(128 + signum)   # the terminal closed: still let the proxy stop when it should

    old = {s: signal.signal(s, hang_up) for s in (signal.SIGHUP, signal.SIGTERM)}
    try:
        argv, env = claude_command(claude, info, config, args, data_dir_given, settings_paths)
        return call(argv, env)
    finally:
        for s, h in old.items():
            signal.signal(s, h)
        with _locked(dir):
            me.unlink(missing_ok=True)
            owner = _owner(dir)
            if owner is not None and not _windows(dir):
                _stop(config, owner)
                (dir / "owner").unlink(missing_ok=True)
