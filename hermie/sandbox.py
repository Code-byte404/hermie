"""macOS Seatbelt (sandbox-exec) execution environment.

- Once applied it is inherited by every child process and cannot be lifted from inside;
- writes: only the workspace and the sandbox temp directory;
- reads: system directories, toolchains, the workspace; the rest of the home directory (~/.ssh, credentials,
  browser data) is never readable;
- network: fully offline;
- open / osascript / security and launching other applications are forbidden (otherwise apps outside the
  sandbox could be used to bypass it);
- the environment is cleared, keeping only PATH/LANG etc.; DEEPSEEK_API_KEY, SSH_AUTH_SOCK and the like never
  enter the sandbox.

Threat model: defends against mistakes of the local model and prompt injection, not against malicious programs
actively trying to escape. The sandbox layer is a replaceable interface (the Executor protocol) so it can be
swapped for a VM-based implementation later.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol

from .config import RunMode, Settings

FSOPS = Path(__file__).resolve().parent / "_fsops.py"

_READ_SUBPATHS = [
    "/usr", "/bin", "/sbin", "/System", "/Library/Developer", "/Library/Frameworks",
    "/Library/Preferences", "/Library/Apple", "/Library/Fonts", "/Library/Filesystems",
    "/private/etc", "/private/var/db/timezone", "/dev", "/opt/homebrew", "/usr/local",
    "/Applications/LibreOffice.app",
]


def _developer_dir() -> list[str]:
    """Command-line tools such as git load from the developer directory that xcode-select points to."""
    try:
        return [os.path.realpath("/var/db/xcode_select_link").split("/Contents/Developer")[0]]
    except OSError:
        return []
_READ_LITERALS = ["/", "/private", "/private/var", "/private/var/select", "/var", "/etc", "/tmp",
                  "/private/var/db/xcode_select_link", "/Library", "/Applications"]
_DENY_EXEC = ["/usr/bin/open", "/usr/bin/osascript", "/usr/bin/security", "/usr/bin/osacompile",
              "/usr/bin/automator", "/usr/bin/shortcuts", "/usr/sbin/screencapture", "/usr/bin/pbcopy",
              "/usr/bin/pbpaste"]
_DENY_MACH = ["com.apple.coreservices.launchservicesd", "com.apple.CoreServices.coreservicesd",
              "com.apple.SecurityServer", "com.apple.securityd", "com.apple.pasteboard.1",
              "com.apple.coreservices.appleevents", "com.apple.windowserver.active",
              "com.apple.lsd.mapdb", "com.apple.lsd.modifydb"]


def _user_temp_dirs() -> list[str]:
    """macOS per-user temp/cache directories (/var/folders/xx/.../T and /C). xcrun, clang and swiftc must write
    their caches there; they count as the "temp directories" the design allows writing to and hold no credentials."""
    out = []
    for key in ("DARWIN_USER_TEMP_DIR", "DARWIN_USER_CACHE_DIR"):
        try:
            d = subprocess.run(["getconf", key], capture_output=True, text=True, timeout=5).stdout.strip()
            if d:
                out.append(os.path.realpath(d))
        except (OSError, subprocess.SubprocessError):
            pass
    return out


def _q(p: str | Path) -> str:
    return json.dumps(str(p))


def _glob_regex(pattern: str) -> str:
    """Turn a file-name glob into a Seatbelt regex fragment (only * and ? are supported; everything else is escaped)."""
    out = []
    for ch in pattern:
        if ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
    return "".join(out)


def _deny_read_rule(workspace: Path, names: tuple) -> Optional[str]:
    """Deny reads by file name inside the workspace (placed after the allow rules: Seatbelt applies the last
    matching rule)."""
    if not names:
        return None
    ws = re.escape(str(workspace))
    regexes = " ".join(f'(regex #"^{ws}/(.*/)?{_glob_regex(n)}$")' for n in names)
    return f"(deny file-read* {regexes})"


def build_profile(workspace: Path, tmpdir: Path, extra_read: list[Path], deny_names: tuple = ()) -> str:
    reads = [*_READ_SUBPATHS, *_developer_dir(), *map(str, extra_read)]
    lines = [
        "(version 1)",
        "(deny default)",
        "(allow process-fork process-exec signal)",
        "(allow sysctl-read ipc-posix-shm ipc-posix-sem file-read-metadata pseudo-tty)",
        "(allow mach-lookup)",
        "(deny mach-lookup " + " ".join(f"(global-name {_q(n)})" for n in _DENY_MACH) + ")",
        "(allow file-read* " + " ".join(f"(subpath {_q(p)})" for p in reads) + " "
        + " ".join(f"(literal {_q(p)})" for p in _READ_LITERALS) + ")",
        "(allow file-read* file-write* " + " ".join(
            f"(subpath {_q(p)})" for p in [workspace, tmpdir, *_user_temp_dirs()]) + ")",
        '(allow file-write* (literal "/dev/null") (literal "/dev/zero") (regex #"^/dev/tty") (regex #"^/dev/fd/"))',
        '(allow file-ioctl (regex #"^/dev/tty"))',
        "(deny network*)",
        "(deny appleevent-send)",
        "(deny process-exec " + " ".join(f"(literal {_q(p)})" for p in _DENY_EXEC) + ")",
    ]
    if deny := _deny_read_rule(workspace, deny_names):
        lines.append(deny)
    return "\n".join(lines) + "\n"


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False

    def combined(self, limit: int) -> str:
        text = self.stdout + (("\n[stderr]\n" + self.stderr) if self.stderr.strip() else "")
        if self.timed_out:
            text += "\n[timed out; process was killed]"
        if len(text) > limit:
            text = text[: limit // 2] + f"\n...({len(text) - limit} chars omitted)...\n" + text[-limit // 2:]
        return text


class Executor(Protocol):
    async def run_shell(self, command: str, timeout: float | None = None) -> ExecResult: ...
    async def fs(self, op: str, **args) -> dict: ...


class Sandbox:
    """Every executor tool goes through here and runs in a subprocess."""

    def __init__(self, settings: Settings):
        self.s = settings
        self.workspace = settings.workspace.resolve()
        self.tmpdir = (settings.data_dir / "sandbox_tmp").resolve()
        self.tmpdir.mkdir(parents=True, exist_ok=True)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.python = Path(sys.executable)
        self.env_prefix = Path(sys.prefix).resolve()
        self.profile_path = (settings.data_dir / "sandbox.sb").resolve()
        self.profile_path.write_text(build_profile(
            self.workspace, self.tmpdir, [self.env_prefix, FSOPS.parent], settings.sandbox_deny_names),
            encoding="utf-8")
        self._procs: set[asyncio.subprocess.Process] = set()

    @property
    def sandboxed(self) -> bool:
        return self.s.mode is not RunMode.NO_SANDBOX

    def _env(self) -> dict[str, str]:
        # The developer directory's bin goes before /usr/bin to bypass the xcrun shims (they write caches
        # outside the sandbox)
        dev = "".join(f"{d}/Contents/Developer/usr/bin:" for d in _developer_dir())
        path = f"{self.env_prefix / 'bin'}:{dev}/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        if not self.sandboxed:
            return {**os.environ, "PATH": path}
        return {"PATH": path, "HOME": str(self.workspace), "TMPDIR": str(self.tmpdir) + "/",
                "LANG": "zh_CN.UTF-8", "LC_ALL": "zh_CN.UTF-8", "PYTHONIOENCODING": "utf-8",
                "TERM": "dumb", "GIT_CONFIG_NOSYSTEM": "1"}

    def _wrap(self, argv: list[str]) -> list[str]:
        if not self.sandboxed:
            return argv
        return ["/usr/bin/sandbox-exec", "-f", str(self.profile_path), *argv]

    async def _exec(self, argv: list[str], stdin: bytes | None, timeout: float) -> ExecResult:
        t0 = time.time()
        proc = await asyncio.create_subprocess_exec(
            *self._wrap(argv), cwd=str(self.workspace), env=self._env(),
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        self._procs.add(proc)
        timed_out = False
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout)
        except asyncio.TimeoutError:
            timed_out = True
            self._kill(proc)
            try:  # the process group got SIGKILL; if a grandchild still holds the pipe, don't wait forever
                out, err = await asyncio.wait_for(proc.communicate(), timeout=5)
            except asyncio.TimeoutError:
                out, err = b"", "[process did not exit in time]".encode()
        except asyncio.CancelledError:  # user pressed Esc to interrupt
            self._kill(proc)
            raise
        finally:
            self._procs.discard(proc)
        return ExecResult(proc.returncode if proc.returncode is not None else -1,
                          out.decode("utf-8", "replace"), err.decode("utf-8", "replace"),
                          round(time.time() - t0, 3), timed_out)

    @staticmethod
    def _kill(proc: asyncio.subprocess.Process) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def kill_all(self) -> None:
        for p in list(self._procs):
            self._kill(p)

    async def run_shell(self, command: str, timeout: float | None = None) -> ExecResult:
        return await self._exec(["/bin/zsh", "-f", "-c", command], None, timeout or self.s.command_timeout_s)

    async def fs(self, op: str, **args) -> dict:
        payload = json.dumps({"root": str(self.workspace), **args}, ensure_ascii=False).encode()
        r = await self._exec([str(self.python), "-I", str(FSOPS), op], payload, 60)
        try:
            return json.loads(r.stdout)
        except json.JSONDecodeError:
            return {"error": f"File operation failed (exit={r.exit_code}): {r.stderr.strip()[-500:]}"}
