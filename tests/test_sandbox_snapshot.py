"""Real Seatbelt sandbox and snapshot rollback (macOS only)."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermie.config import RunMode
from hermie.sandbox import Sandbox
from hermie.snapshot import SnapshotManager

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is only available on macOS")


@pytest.fixture
def sb(settings):
    settings.ensure_dirs()
    return Sandbox(settings)


async def test_workspace_rw_allowed(sb):
    r = await sb.run_shell("echo hello > a.txt && cat a.txt")
    assert r.exit_code == 0 and "hello" in r.stdout
    assert (await sb.fs("write", path="d/b.txt", content="x"))["bytes"] == 1
    assert (await sb.fs("read", path="d/b.txt"))["content"] == "x"


async def test_write_outside_denied(sb):
    # tmp_path cannot be used: it lives under the per-user temp dir, which is writable (toolchain caches)
    target = Path.home() / ".hermie_escape_test.txt"
    target.unlink(missing_ok=True)
    try:
        r = await sb.run_shell(f"echo x > {target}")
        assert r.exit_code != 0 and not target.exists()
        res = await sb.fs("write", path=str(target), content="x")
        assert "error" in res and not target.exists()
        r = await sb.run_shell(f"echo x > {Path.home() / 'Documents' / '.hermie_escape_test.txt'}")
        assert r.exit_code != 0
    finally:
        target.unlink(missing_ok=True)


async def test_home_secrets_unreadable(sb):
    home = os.path.expanduser("~")
    r = await sb.run_shell(f"ls {home}/.ssh {home}/Library/Keychains {home}/Documents")
    assert r.exit_code != 0 and "Operation not permitted" in r.stderr


async def test_network_allowed_and_caches_stay_out_of_the_workspace(sb):
    # A local server instead of the internet: the test must not depend on connectivity
    import asyncio

    async def serve(reader, writer):
        await reader.readline()
        writer.write(b"HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\nok")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        r = await sb.run_shell(f"curl -sS -m 5 http://127.0.0.1:{port}/")
        assert r.exit_code == 0 and r.stdout == "ok", r.stderr
    finally:
        server.close()
    env = sb._env()
    for key in ("npm_config_cache", "PIP_CACHE_DIR", "XDG_CACHE_HOME"):
        assert Path(env[key]).is_relative_to(sb.tmpdir), key


async def test_gui_apps_and_keychain_denied(sb):
    # screencapture stays denied inside the shell: Mac screenshots go through the screenshot tool (controller process)
    for cmd in ("open .", "osascript -e 'return 1'", "security list-keychains", "screencapture -x shot.png"):
        r = await sb.run_shell(cmd)
        assert r.exit_code != 0, cmd


async def test_env_scrubbed(sb, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-secret")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    r = await sb.run_shell("env")
    assert "sk-secret" not in r.stdout and "SSH_AUTH_SOCK" not in r.stdout


async def test_timeout_kills(sb, settings):
    r = await sb.run_shell("sleep 30", timeout=1)
    assert r.timed_out and r.duration_s < 5


async def test_toolchain_available(sb):
    r = await sb.run_shell("python -c 'import openpyxl, docx; print(1)' && git --version")
    assert r.exit_code == 0, r.stderr


async def test_no_sandbox_mode_lifts_boundary(sb, settings, tmp_path):
    settings.mode = RunMode.NO_SANDBOX
    target = tmp_path / "outside.txt"
    r = await sb.run_shell(f"echo x > {target}")
    assert r.exit_code == 0 and target.exists()


def test_clone_snapshot_restore(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("original")
    m = SnapshotManager(ws, tmp_path / "snaps")
    snap = m.take()
    assert snap.kind == "clone"
    (ws / "a.txt").write_text("broken")
    (ws / "new.txt").write_text("new file")
    m.restore(m.latest())
    assert (ws / "a.txt").read_text() == "original" and not (ws / "new.txt").exists()


def test_git_snapshot_restore_keeps_history(tmp_path):
    ws = tmp_path / "repo"
    ws.mkdir()
    git = lambda *a: subprocess.run(["git", "-C", str(ws), *a], check=True, capture_output=True, text=True).stdout
    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (ws / "a.py").write_text("v1")
    git("add", ".")
    git("commit", "-qm", "init")
    (ws / "untracked.txt").write_text("draft")
    head_before = git("rev-parse", "HEAD")
    m = SnapshotManager(ws, tmp_path / "snaps")
    snap = m.take()
    assert snap.kind == "git"
    (ws / "a.py").write_text("broken")
    (ws / "untracked.txt").unlink()
    (ws / "junk.txt").write_text("x")
    m.restore(snap)
    assert (ws / "a.py").read_text() == "v1"
    assert (ws / "untracked.txt").read_text() == "draft"
    assert not (ws / "junk.txt").exists()
    assert git("rev-parse", "HEAD") == head_before  # the user's branch and history were not touched
    assert "hermie" not in git("log", "--oneline")


async def test_swift_toolchain_can_write_its_caches(sb):
    """swiftc / xcrun need to write the per-user temp and cache dirs; skipped when the Xcode CLT is not installed."""
    (sb.workspace / "hello.swift").write_text('print("hi")\n')
    r = await sb.run_shell("swiftc -parse hello.swift")
    if r.exit_code == 127:
        pytest.skip("swiftc not available")
    assert r.exit_code == 0 and "couldn't create cache file" not in r.stderr, r.stderr


async def test_workspace_credential_files_unreadable(sb, settings):
    ws = settings.workspace
    (ws / "certs").mkdir(parents=True, exist_ok=True)
    for name in ("id_rsa", "certs/server.pem", "certs/tls.key", ".netrc"):
        (ws / name).write_text("SECRET")
    (ws / "notes.txt").write_text("ok")
    for name in ("id_rsa", "certs/server.pem", "certs/tls.key", ".netrc"):
        r = await sb.run_shell(f"cat {name}")
        assert r.exit_code != 0 and "SECRET" not in r.stdout, name
        assert "error" in await sb.fs("read", path=name), name
    assert (await sb.fs("read", path="notes.txt"))["content"] == "ok"
    r = await sb.run_shell("cat notes.txt && ls certs")   # metadata stays readable: can list, cannot read content
    assert r.exit_code == 0 and "server.pem" in r.stdout


async def test_fsops_refuses_paths_outside_workspace(sb, settings):
    for path in ("/etc/hosts", "../outside.txt", "/private/tmp/x.txt", str(settings.data_dir / "audit.jsonl")):
        res = await sb.fs("read", path=path)
        assert "error" in res and "outside the workspace" in res["error"], path
        res = await sb.fs("write", path=path, content="x")
        assert "error" in res and "outside the workspace" in res["error"], path
    assert "error" in await sb.fs("list", path="..")
    (settings.workspace / "link").symlink_to("/etc")
    assert "outside the workspace" in (await sb.fs("read", path="link/hosts"))["error"]


async def test_fsops_boundary_holds_without_sandbox(settings):
    settings.mode = RunMode.NO_SANDBOX
    settings.ensure_dirs()
    sb = Sandbox(settings)
    assert "outside the workspace" in (await sb.fs("read", path="/etc/hosts"))["error"]
    assert (await sb.fs("write", path="ok.txt", content="1"))["bytes"] == 1


def test_prune_keeps_recent_clone_snapshots(tmp_path):
    ws, store = tmp_path / "ws", tmp_path / "store"
    ws.mkdir()
    (ws / "a.txt").write_text("1")
    m = SnapshotManager(ws, store)
    snaps = [m.take(label="task") for _ in range(4)]
    m.take(label="step", keep=3)                      # take() prunes automatically
    remaining = m.list()
    assert [x.id for x in remaining] == [snaps[2].id, snaps[3].id, remaining[-1].id]
    assert not Path(snaps[0].ref).exists() and not Path(snaps[1].ref).exists() and Path(snaps[3].ref).exists()
    dropped = m.prune(1)
    assert len(dropped) == 2 and len(m.list()) == 1 and m.list()[0].label == "step"


def test_prune_git_snapshots_drops_refs(tmp_path):
    ws, store = tmp_path / "ws", tmp_path / "store"
    ws.mkdir()
    subprocess.run(["git", "init", "-q", str(ws)], check=True)
    (ws / "a.txt").write_text("1")
    m = SnapshotManager(ws, store)
    ids = [m.take().id for _ in range(3)]
    m.prune(1)
    refs = subprocess.run(["git", "-C", str(ws), "for-each-ref", "refs/hermie/"], capture_output=True, text=True).stdout
    assert ids[2] in refs and ids[0] not in refs and ids[1] not in refs
    assert [x.id for x in m.list()] == [ids[2]]


# ---------------- Commands that stop and wait for input

from hermie.sandbox import looks_like_prompt


def test_looks_like_prompt():
    assert looks_like_prompt("Project name? ")                    # no trailing newline: the process is mid-line
    assert looks_like_prompt("Need to install create-next-app\nOk to proceed? (y)\n")
    assert looks_like_prompt("Overwrite? [y/N]\n")
    assert looks_like_prompt("\x1b[36m?\x1b[0m Would you like to use TypeScript? \x1b[90m› No / Yes\x1b[0m\n")
    assert not looks_like_prompt("compiling...\nstill compiling\n")
    assert not looks_like_prompt("")


@pytest.fixture
def isb(settings):
    settings.ensure_dirs()
    settings.command_idle_s = 0.4
    return Sandbox(settings)


async def test_prompt_is_answered_through_ask(isb):
    asked = []

    async def ask(tail):
        asked.append(tail)
        return "bob"

    r = await isb.run_shell('printf "Your name? "; read x; echo "hi $x"', timeout=20, ask=ask)
    assert r.exit_code == 0 and "hi bob" in r.stdout
    assert len(asked) == 1 and "Your name?" in asked[0]
    assert "bob" in r.combined(4000)          # the model sees what was answered


async def test_several_prompts_in_a_row(isb):
    answers = iter(["a", "b"])

    async def ask(tail):
        return next(answers)

    r = await isb.run_shell('printf "one? "; read x; printf "two? "; read y; echo "$x$y"', timeout=20, ask=ask)
    assert r.exit_code == 0 and r.stdout.strip().endswith("ab")


async def test_prompt_without_asker_gets_eof(isb):
    r = await isb.run_shell('printf "Your name? "; read x || echo EOF_SEEN', timeout=20)
    assert r.exit_code == 0 and "EOF_SEEN" in r.stdout and r.duration_s < 5


async def test_user_can_stop_a_waiting_command(isb):
    async def ask(tail):
        return None

    r = await isb.run_shell('printf "Continue? (y/n) "; read x; echo "went on"', timeout=20, ask=ask)
    assert "went on" not in r.stdout and r.duration_s < 5
    assert "stopped by the user" in r.combined(4000)


async def test_prompt_that_ignores_eof_is_killed_with_a_hint(isb):
    r = await isb.run_shell('printf "Continue? (y/n) "; sleep 30', timeout=20)
    assert r.exit_code != 0 and r.duration_s < 5
    assert "non-interactive" in r.combined(4000)


async def test_silent_command_is_not_mistaken_for_a_prompt(isb):
    async def ask(tail):
        raise AssertionError("should not ask")

    r = await isb.run_shell("echo building; sleep 1.5; echo done", timeout=20, ask=ask)
    assert r.exit_code == 0 and "done" in r.stdout and not r.timed_out


async def test_waiting_for_the_user_does_not_count_toward_timeout(isb):
    import asyncio

    async def ask(tail):
        await asyncio.sleep(1.5)
        return "y"

    r = await isb.run_shell('printf "ok? "; read x; echo "got $x"', timeout=1.2, ask=ask)
    assert not r.timed_out and "got y" in r.stdout


async def test_stdin_data_path_still_works(isb):
    assert (await isb.fs("write", path="x.txt", content="hi"))["bytes"] == 2


@pytest.fixture
def attached_dir():
    # Under the home dir: tmp_path is in the per-user temp dir, which the sandbox can read and write anyway
    d = Path.home() / ".hermie_attach_test"
    import shutil
    shutil.rmtree(d, ignore_errors=True)
    (d / "src").mkdir(parents=True)
    (d / "src" / "main.py").write_text("print('attached')")
    (d / ".env").write_text("TOKEN=abc")
    (d / "id_rsa").write_text("KEY")
    yield d
    shutil.rmtree(d, ignore_errors=True)


async def test_attached_dir_is_readable_only_during_the_grant(sb, attached_dir):
    main = attached_dir / "src" / "main.py"
    assert (await sb.run_shell(f"cat {main}")).exit_code != 0
    assert "error" in await sb.fs("read", path=str(main))
    with sb.grant_read([attached_dir]) as granted:
        assert granted == (attached_dir.resolve(),)
        r = await sb.run_shell(f"cat {main}")
        assert r.exit_code == 0 and "attached" in r.stdout
        assert (await sb.fs("read", path=str(main)))["content"] == "print('attached')"
        listed = [f["path"] for f in (await sb.fs("list", path=str(attached_dir)))["files"]]
        assert str(main) in listed
        # read-only: no writes, and credential / .env files stay unreadable
        assert (await sb.run_shell(f"echo x > {attached_dir / 'new.txt'}")).exit_code != 0
        assert "error" in await sb.fs("write", path=str(attached_dir / "new.txt"), content="x")
        assert "error" in await sb.fs("edit", path=str(main), old="attached", new="changed")
        assert not (attached_dir / "new.txt").exists() and "attached" in main.read_text()
        for secret in (".env", "id_rsa"):
            assert (await sb.run_shell(f"cat {attached_dir / secret}")).exit_code != 0, secret
        # a sibling of the attached dir is not covered
        assert (await sb.run_shell(f"ls {Path.home() / 'Documents'}")).exit_code != 0
    assert (await sb.run_shell(f"cat {main}")).exit_code != 0
    assert "error" in await sb.fs("read", path=str(main))


def test_grant_read_refuses_broad_or_secret_paths(sb, attached_dir):
    home = Path.home()
    refused = [Path("/"), home, home.parent, sb.workspace, attached_dir / "id_rsa", attached_dir / ".env",
               attached_dir / "missing"]
    assert not any(sb.grantable(p) for p in refused)
    with sb.grant_read(refused) as granted:
        assert granted == ()
    assert sb.grantable(attached_dir) and sb.grantable(attached_dir / "src" / "main.py")


_CONNECT = "python3 -c \"import socket; socket.create_connection(('1.1.1.1', 53), 3)\""
_RESOLVE = "python3 -c \"import socket, uuid; socket.getaddrinfo(uuid.uuid4().hex[:12] + '.example.com', 80)\""


async def test_offline_profile_blocks_network_and_dns(settings):
    from hermie.sandbox import Sandbox
    sb = Sandbox(settings)
    # Positive control: online, the same commands must work, or this machine has no network to block
    if (await sb.run_shell(_CONNECT)).exit_code != 0 or \
            (await sb.run_shell("python3 -c \"import socket; socket.getaddrinfo('example.com', 80)\"")).exit_code != 0:
        pytest.skip("no network on this machine: cannot show that offline blocks it")
    sb.set_offline(True)
    text = sb.profile_path.read_text()
    assert '(deny network-outbound (remote ip "*:*"))' in text and "com.apple.dnssd.service" in text
    assert "mDNSResponder" in text
    r = await sb.run_shell(_CONNECT)
    assert r.exit_code != 0
    r = await sb.run_shell("python3 -c \"import socket; socket.getaddrinfo('example.com', 80)\"")
    assert r.exit_code != 0
    r = await sb.run_shell(_RESOLVE)   # a name never resolved before: no cache can answer it
    assert r.exit_code != 0
    r = await sb.run_shell("echo still-works")
    assert r.exit_code == 0 and "still-works" in r.stdout
    sb.set_offline(False)
    assert "(deny network-outbound" not in sb.profile_path.read_text()


def test_each_sandbox_has_its_own_profile(settings):
    a, b = Sandbox(settings), Sandbox(settings)
    assert a.profile_path != b.profile_path and a.profile_path.parent == b.profile_path.parent
    a.set_offline(True)
    b.set_offline(True)
    b.set_offline(False)    # another Hermie finishing its task must not put this one back online
    assert "(deny network-outbound" in a.profile_path.read_text()
    assert "(deny network-outbound" not in b.profile_path.read_text()
    a.close()
    assert not a.profile_path.exists() and b.profile_path.exists()


def test_stale_profiles_are_pruned(settings):
    settings.ensure_dirs()
    dead = subprocess.Popen(["/usr/bin/true"])
    dead.wait()
    stale = settings.data_dir / f"sandbox-{dead.pid}-deadbeef.sb"
    live = settings.data_dir / f"sandbox-{os.getpid()}-cafe0000.sb"
    stale.write_text("(version 1)")
    live.write_text("(version 1)")
    Sandbox(settings)
    assert not stale.exists() and live.exists()


async def test_going_offline_stops_running_commands(settings):
    import asyncio
    sb = Sandbox(settings)
    run = asyncio.ensure_future(sb.run_shell("sleep 30; echo finished", timeout=60))
    for _ in range(100):
        if sb._procs:
            break
        await asyncio.sleep(0.05)
    assert sb._procs
    sb.set_offline(True)
    r = await asyncio.wait_for(run, 10)
    assert "finished" not in r.stdout and r.exit_code != 0


async def test_asc_binary_exec_denied_in_profile(settings, tmp_path):
    """asc reads the keychain-backed App Store Connect account: inside the sandbox it must not run at all (the
    executor uses the asc tool instead). A script named asc stands in for the real binary."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake_asc, other = bindir / "asc", bindir / "notasc"
    for f in (fake_asc, other):
        f.write_text('#!/bin/sh\necho "$@"\n')
        f.chmod(0o755)
    settings.asc_path = str(fake_asc)
    settings.ensure_dirs()
    sb = Sandbox(settings)
    profile = sb.profile_path.read_text()
    deny = next(l for l in profile.splitlines() if l.startswith("(deny process-exec"))
    for p in (str(fake_asc), "/opt/homebrew/bin/asc", "/usr/local/bin/asc"):
        assert f'(literal "{p}")' in deny, p
    r = await sb.run_shell(f"{other} hello")
    assert r.exit_code == 0 and "hello" in r.stdout          # control: the same binary under another name runs
    r = await sb.run_shell(f"{fake_asc} hello")
    assert r.exit_code != 0 and "hello" not in r.stdout
    r = await sb.run_shell(f"cp {fake_asc} ./copied && ./copied hello")   # nor copied under another name
    assert r.exit_code != 0 and "hello" not in r.stdout


def test_p8_keys_denied_by_default():
    from hermie.config import Settings
    assert "*.p8" in Settings().sandbox_deny_names


def test_absurd_pid_in_stale_profile_name_does_not_crash_startup(settings):
    settings.ensure_dirs()
    bogus = settings.data_dir / f"sandbox-{'9' * 40}-abcdef12.sb"
    bogus.write_text("(version 1)")
    sb = Sandbox(settings)
    assert sb.profile_path.exists() and not bogus.exists()


async def test_sandbox_cannot_read_google_credentials_dir(settings):
    """ADC lives under ~/.config/gcloud; nothing under ~/.config is readable from the sandbox."""
    import shutil
    import uuid
    probe = Path.home() / ".config" / f"hermie-test-{uuid.uuid4().hex[:8]}"
    probe.mkdir(parents=True)
    try:
        (probe / "application_default_credentials.json").write_text('{"refresh_token": "x"}')
        sb = Sandbox(settings)
        r = await sb.run_shell(f"cat {probe}/application_default_credentials.json")
        assert r.exit_code != 0 and "refresh_token" not in r.stdout
    finally:
        shutil.rmtree(probe, ignore_errors=True)
