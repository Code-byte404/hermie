"""macOS Seatbelt (sandbox-exec) execution environment.

- Once applied it is inherited by every child process and cannot be lifted from inside;
- writes: only the workspace and the sandbox temp directory;
- reads: system directories, toolchains, the workspace; the rest of the home directory (~/.ssh, credentials,
  browser data) is never readable, except files and directories the user attached to the current task
  (`Sandbox.grant_read`), which are readable, never writable, for that task only;
- network: open (package installs, git clones, API calls from the user's own code), except during business-data
  tasks (connectors), when remote connections and DNS are denied. The executor's commands can otherwise
  send data out; curl / wget / ssh / pip install stay high-risk commands that need approval in default
  mode (capabilities.rule_risk), and the executor's own web tools still go through the outbound check;
- asc (App Store Connect / Apple Ads) never runs here: only the asc connector tool runs it, in the main process;
- open / osascript / security and launching other applications are forbidden (otherwise apps outside the
  sandbox could be used to bypass it);
- the environment is cleared, keeping only PATH/LANG etc.; CLOUD_API_KEY, SSH_AUTH_SOCK and the like never
  enter the sandbox.

Threat model: defends against mistakes of the local model and prompt injection, not against malicious programs
actively trying to escape. The sandbox layer is a replaceable interface (the Executor protocol) so it can be
swapped for a VM-based implementation later.
"""
from __future__ import annotations

import asyncio
import atexit
import fnmatch
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Iterable, Iterator, Optional, Protocol

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
# asc reads the user's App Store Connect / Apple Ads account (keychain, or a ./.asc config + .p8 key): it runs only
# through the connector in the main process, never inside the sandbox
ASC_COMMON_PATHS = ("/opt/homebrew/bin/asc", "/usr/local/bin/asc")


def asc_binaries(settings: Settings) -> list[str]:
    """Every path the asc binary may be exec'd by: ASC_PATH, the one on PATH, the usual Homebrew locations, and
    the real file behind each symlink."""
    import shutil
    paths = [p for p in (settings.asc_path, shutil.which("asc"), *ASC_COMMON_PATHS) if p]
    out = []
    for p in paths:
        for q in (os.path.abspath(os.path.expanduser(p)), os.path.realpath(os.path.expanduser(p))):
            if q not in out:
                out.append(q)
    return out


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


# Also unreadable inside attached paths: another project's .env is a secret, and nothing is built there
_ATTACHED_DENY_NAMES = (".env", ".env.*")


def _deny_read_rule(root: Path, names: tuple) -> Optional[str]:
    """Deny reads by file name under root (placed after the allow rules: Seatbelt applies the last matching rule)."""
    if not names:
        return None
    r = re.escape(str(root))
    regexes = " ".join(f'(regex #"^{r}(/.*)?/{_glob_regex(n)}$")' for n in names)
    return f"(deny file-read* {regexes})"


def build_profile(workspace: Path, tmpdir: Path, extra_read: list[Path], deny_names: tuple = (),
                  attached: Iterable[Path] = (), network: bool = True, deny_exec: Iterable[str] = ()) -> str:
    attached = list(attached)
    reads = [*_READ_SUBPATHS, *_developer_dir(), *map(str, extra_read), *map(str, attached)]
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
        "(allow network*)",
        "(deny appleevent-send)",
        "(deny process-exec " + " ".join(f"(literal {_q(p)})" for p in (*_DENY_EXEC, *deny_exec)) + ")",
    ]
    if not network:   # business data: no remote connections and no DNS (a hostname can carry data)
        lines.append('(deny network-outbound (remote ip "*:*"))')
        lines.append('(deny mach-lookup (global-name "com.apple.dnssd.service"))')
        # getaddrinfo talks to mDNSResponder over this unix socket, not over the mach service (measured on macOS 26)
        lines.append('(deny network-outbound (remote unix-socket (path-literal "/private/var/run/mDNSResponder")))')
    if deny_exec:   # not readable either, so the binary cannot be copied to a path the exec rule does not cover
        lines.append("(deny file-read* " + " ".join(f"(literal {_q(p)})" for p in deny_exec) + ")")
    if deny := _deny_read_rule(workspace, deny_names):
        lines.append(deny)
    for root in attached:
        if deny := _deny_read_rule(root, (*deny_names, *_ATTACHED_DENY_NAMES)):
            lines.append(deny)
    return "\n".join(lines) + "\n"


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False
    notes: list[str] = field(default_factory=list)   # what happened around input prompts, shown to the model

    def combined(self, limit: int) -> str:
        text = self.stdout + (("\n[stderr]\n" + self.stderr) if self.stderr.strip() else "")
        if self.timed_out:
            text += "\n[timed out; process was killed]"
        text += "".join(f"\n{n}" for n in self.notes)
        if len(text) > limit:
            text = text[: limit // 2] + f"\n...({len(text) - limit} chars omitted)...\n" + text[-limit // 2:]
        return text


# Given the tail of a command's output, returns the line to type into it, or None to stop the command
InputAsker = Callable[[str], Awaitable[Optional[str]]]

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-_]")
# a question-mark line (inquirer/prompts style "? Name ›", or "...? (y)" / "[y/N]" / "› No / Yes" after the "?"),
# an explicit yes/no or key prompt, or a password prompt
_PROMPT_LINE = re.compile(r"^\?\s|\?\s*(\(.{0,20}\)|\[.{0,20}\]|›.{0,40})?\s*$|\((y/n|yes/no)\)|\[y/n\]"
                          r"|press (enter|return|any key)|password|passphrase", re.I)
NON_INTERACTIVE_HINT = ("[the command kept waiting for input after its stdin was closed: {line!r}. Rerun it with "
                        "non-interactive flags (e.g. --yes / -y, CI=1) or pipe the answers in]")


def looks_like_prompt(tail: str) -> bool:
    """Whether output that has gone quiet ends on something that asks for input: a partial line (the cursor is
    still on it), or a last line that reads like a question or a yes/no prompt."""
    text = _ANSI.sub("", tail).replace("\r", "\n")
    if not text.strip():
        return False
    if not text.endswith("\n"):
        return True
    last = text.rstrip().rsplit("\n", 1)[-1].strip()
    return bool(_PROMPT_LINE.search(last))


def _tail(buf: bytes, lines: int = 20) -> str:
    return "\n".join(buf[-8000:].decode("utf-8", "replace").split("\n")[-lines:])


class Executor(Protocol):
    async def run_shell(self, command: str, timeout: float | None = None,
                        ask: Optional[InputAsker] = None) -> ExecResult: ...
    async def fs(self, op: str, **args) -> dict: ...


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:   # exists, owned by someone else
        return True
    except (OverflowError, ValueError):   # not a real pid (absurd number in a stale file name): nothing to keep
        return False
    return True


def _prune_stale_profiles(data_dir: Path) -> None:
    """Delete sandbox-<pid>-<id>.sb files left behind by processes that are gone (crash, kill -9)."""
    try:
        files = list(data_dir.glob("sandbox-*.sb"))
    except OSError:
        return
    for f in files:
        m = re.fullmatch(r"sandbox-(\d+)-[0-9a-f]+\.sb", f.name)
        if m and not _pid_alive(int(m.group(1))):
            _unlink_quietly(f)


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
        # One profile file per instance: sandbox-exec reads it at every launch, so a shared file would let another
        # Hermie on the same data_dir rewrite it (and e.g. undo this instance's offline lock)
        _prune_stale_profiles(settings.data_dir)
        self.profile_path = (settings.data_dir / f"sandbox-{os.getpid()}-{uuid.uuid4().hex[:8]}.sb").resolve()
        atexit.register(_unlink_quietly, self.profile_path)
        self.read_roots: tuple[Path, ...] = ()   # paths attached to the running task: read-only
        self.room: Optional[Path] = None         # the business task's data room: read-only (grant_room)
        self.offline = False                     # business-data task: no network (set_offline)
        self._procs: set[asyncio.subprocess.Process] = set()
        self.deny_exec = asc_binaries(settings)
        self._write_profile()

    def _write_profile(self) -> None:
        self.profile_path.write_text(build_profile(
            self.workspace, self.tmpdir, [self.env_prefix, FSOPS.parent], self.s.sandbox_deny_names,
            attached=self._granted(), network=not self.offline, deny_exec=self.deny_exec), encoding="utf-8")

    def _granted(self) -> tuple[Path, ...]:
        return (*self.read_roots, *((self.room,) if self.room is not None else ()))

    def grant_room(self, room: Path) -> None:
        """The data room of a task that has turned business becomes readable (never writable) to the sandbox, by
        the profile and by _fsops, until revoke_room at task end. Independent of grant_read (user attachments)."""
        room = room.resolve()
        if room != self.room:
            self.room = room
            self._write_profile()

    def revoke_room(self) -> None:
        if self.room is not None:
            self.room = None
            self._write_profile()

    def set_offline(self, flag: bool) -> None:
        """Business-data tasks: the sandbox gets no network. Commands still running were started with the online
        profile, so going offline stops them."""
        if flag != self.offline:
            self.offline = flag
            self._write_profile()
            if flag:
                self.kill_all()

    def close(self) -> None:
        """Remove this instance's profile file (best-effort; also registered with atexit)."""
        _unlink_quietly(self.profile_path)

    def grantable(self, path: Path) -> bool:
        """Whether an attached path may be opened read-only: not the root, not the home directory or anything
        containing it, not already inside the workspace (readable anyway), not a credential or .env file."""
        try:
            p = path.resolve(strict=True)
        except OSError:
            return False
        home = Path.home().resolve()
        if p == Path("/") or p in (home, *home.parents) or p.is_relative_to(self.workspace):
            return False
        return not (p.is_file() and any(fnmatch.fnmatch(p.name, n)
                                        for n in (*self.s.sandbox_deny_names, *_ATTACHED_DENY_NAMES)))

    def writable(self, path: Path) -> bool:
        """Whether sandboxed commands may write to path: the workspace, the sandbox temp dir and the per-user
        temp/cache dirs are writable (build_profile); everything else is not. Always True without the sandbox."""
        if not self.sandboxed:
            return True
        p = Path(os.path.realpath(path))
        return any(p.is_relative_to(os.path.realpath(r)) for r in (self.workspace, self.tmpdir, *_user_temp_dirs()))

    @contextmanager
    def grant_read(self, paths: Iterable[Path]) -> Iterator[tuple[Path, ...]]:
        """Make the files/directories the user attached to a task readable (never writable) for its duration:
        by the Seatbelt profile for commands, by _fsops for read_file / list_files. Yields what was granted."""
        roots = tuple(dict.fromkeys(p.resolve() for p in paths if self.grantable(p)))
        if not roots:
            yield ()
            return
        previous, self.read_roots = self.read_roots, roots
        self._write_profile()
        try:
            yield roots
        finally:
            self.read_roots = previous
            self._write_profile()

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
        cache = self.tmpdir / "cache"   # HOME is the workspace: keep package-manager caches out of it
        return {"PATH": path, "HOME": str(self.workspace), "TMPDIR": str(self.tmpdir) + "/",
                "XDG_CACHE_HOME": str(cache), "npm_config_cache": str(cache / "npm"),
                "npm_config_store_dir": str(cache / "pnpm-store"), "PIP_CACHE_DIR": str(cache / "pip"),
                "YARN_CACHE_FOLDER": str(cache / "yarn"), "CARGO_HOME": str(cache / "cargo"),
                "GOPATH": str(cache / "go"), "GOMODCACHE": str(cache / "go" / "mod"),
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

    async def run_shell(self, command: str, timeout: float | None = None,
                        ask: Optional[InputAsker] = None) -> ExecResult:
        """Run a shell command, reading its output as it comes. When it goes quiet on a prompt, `ask` (if any)
        supplies the answer; otherwise its stdin is closed, and a command still waiting on the prompt after that
        is stopped with a hint instead of hanging until the timeout."""
        return await self._interactive(["/bin/zsh", "-f", "-c", command], timeout or self.s.command_timeout_s, ask)

    async def _interactive(self, argv: list[str], timeout: float, ask: Optional[InputAsker]) -> ExecResult:
        t0 = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *self._wrap(argv), cwd=str(self.workspace), env=self._env(), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        self._procs.add(proc)
        bufs = {"out": bytearray(), "err": bytearray()}
        state = {"last": time.monotonic(), "stream": "out"}

        async def pump(stream: asyncio.StreamReader, key: str) -> None:
            while chunk := await stream.read(4096):
                bufs[key] += chunk
                state["last"], state["stream"] = time.monotonic(), key

        pumps = [asyncio.create_task(pump(proc.stdout, "out")), asyncio.create_task(pump(proc.stderr, "err"))]
        waiter = asyncio.create_task(proc.wait())
        deadline, idle = t0 + timeout, max(self.s.command_idle_s, 0.1)
        timed_out, notes = False, []
        stdin_open, eof_prompt = True, None   # eof_prompt: the prompt line stdin was closed on

        def close_stdin() -> None:
            try:
                proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                pass

        try:
            while True:
                wait = min(deadline, state["last"] + idle) - time.monotonic()
                await asyncio.wait({waiter}, timeout=max(wait, 0.01))
                if waiter.done():
                    break
                now = time.monotonic()
                if now >= deadline:
                    timed_out = True
                    self._kill(proc)
                    break
                if now - state["last"] < idle:
                    continue
                tail = _tail(bufs[state["stream"]])
                prompt = looks_like_prompt(tail)
                if prompt and stdin_open and ask is not None:
                    asked_at = time.monotonic()
                    reply = await ask(tail)
                    deadline += time.monotonic() - asked_at   # the user's thinking time is not the command's
                    if reply is None:
                        notes.append("[stopped by the user while the command was waiting for input]")
                        self._kill(proc)
                        break
                    notes.append(f"[the command asked for input; the user typed: {reply!r}]")
                    try:
                        proc.stdin.write(reply.encode() + b"\n")
                        await proc.stdin.drain()
                    except (BrokenPipeError, ConnectionResetError):
                        stdin_open = False
                elif stdin_open:   # nobody to answer, or just silent: give it EOF, as a non-interactive run would
                    close_stdin()
                    stdin_open = False
                    if prompt:
                        eof_prompt = _ANSI.sub("", tail).strip().rsplit("\n", 1)[-1].strip()
                elif eof_prompt is not None:
                    notes.append(NON_INTERACTIVE_HINT.format(line=eof_prompt[:200]))
                    self._kill(proc)
                    break
                state["last"] = time.monotonic()
        except asyncio.CancelledError:  # user pressed Esc to interrupt
            self._kill(proc)
            raise
        finally:
            self._procs.discard(proc)
            try:  # after a kill a grandchild may still hold the pipes: don't wait forever for them
                await asyncio.wait_for(asyncio.gather(waiter, *pumps, return_exceptions=True), timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                for t in (waiter, *pumps):
                    t.cancel()
                if not bufs["err"]:
                    bufs["err"] += b"[process did not exit in time]"
        return ExecResult(proc.returncode if proc.returncode is not None else -1,
                          bufs["out"].decode("utf-8", "replace"), bufs["err"].decode("utf-8", "replace"),
                          round(time.monotonic() - t0, 3), timed_out, notes)

    async def fs(self, op: str, **args) -> dict:
        payload = json.dumps({"root": str(self.workspace), "read_roots": [str(p) for p in self._granted()], **args},
                             ensure_ascii=False).encode()
        r = await self._exec([str(self.python), "-I", str(FSOPS), op], payload, 60)
        try:
            return json.loads(r.stdout)
        except json.JSONDecodeError:
            return {"error": f"File operation failed (exit={r.exit_code}): {r.stderr.strip()[-500:]}"}
