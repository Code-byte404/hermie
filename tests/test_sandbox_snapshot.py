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


async def test_network_denied(sb):
    r = await sb.run_shell("curl -sS -m 5 https://api.deepseek.com")
    assert r.exit_code != 0


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
