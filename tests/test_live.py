"""Live smoke tests: real clients through the real proxy to the real upstreams.

Run one at a time with `pytest -m live tests/test_live.py::test_claude_code -s`.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.live
DEMO = Path(__file__).parent.parent / "demo"
REAL = ["555-010-0101", "sk_test_hermieDemoNotARealKey000000", "demo-password-not-real", "demo-session-secret-0000"]
PLACEHOLDERS = ["<PHONE_NUMBER_", "<CREDIT_CARD_", "<EMAIL_ADDRESS_", "<SECRET_"]
TASK = "Read customers.csv and .env, then tell me how many customers there are. Do not modify files."


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def proxy(tmp_path):
    port = _free_port()
    data = tmp_path / "data"
    log_path = tmp_path / "proxy.log"
    log = open(log_path, "w")
    p = subprocess.Popen(
        [sys.executable, "-m", "hermie.cli", "serve", "--port", str(port), "--mode", "enforce",
         "--data-dir", str(data)],
        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, text=True)
    up = False
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            up = True
            break
        except OSError:
            time.sleep(0.2)
    if not up:
        p.terminate()
        pytest.fail("proxy did not start:\n" + log_path.read_text())
    yield port, data, log_path
    p.terminate()
    try:
        p.wait(5)
    except subprocess.TimeoutExpired:
        p.kill()
    log.close()
    print("\n--- proxy log ---\n" + log_path.read_text()[-3000:])


def _bodies(data_dir):
    return list((data_dir / "outbound").glob("*.json"))


def _assert_clean(data_dir, label):
    bodies = _bodies(data_dir)
    assert bodies, "no request went through the proxy"
    text = "".join(b.read_text() for b in bodies)
    for v in REAL:
        assert v not in text, v
    assert any(ph in text for ph in PLACEHOLDERS), "no placeholder found in any stored body"
    lines = [json.loads(x) for x in (data_dir / "receipt.jsonl").read_text().splitlines() if x.strip()]
    clients = {x.get("client") for x in lines}
    assert label in clients, f"client labels seen: {clients}"


def _repo(tmp_path):
    ws = tmp_path / "ws"
    shutil.copytree(DEMO, ws, ignore=shutil.ignore_patterns("README.md", "record.sh", "tests", "__pycache__"))
    return ws


def _env(**extra):
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE" and not k.startswith("CLAUDE_CODE_")}
    env.update(extra)
    return env


@pytest.mark.skipif(not shutil.which("claude"), reason="claude not installed")
def test_claude_code(proxy, tmp_path):
    port, data, _ = proxy
    ws = _repo(tmp_path)
    env = _env(ANTHROPIC_BASE_URL=f"http://127.0.0.1:{port}/anthropic")
    r = subprocess.run(["claude", "-p", TASK, "--permission-mode", "bypassPermissions", "--output-format", "text"],
                       cwd=ws, env=env, capture_output=True, text=True, timeout=600)
    print("claude rc", r.returncode, "stdout:", r.stdout[-800:], "stderr:", r.stderr[-800:])
    if r.returncode != 0 and not _bodies(data) and ("auth" in (r.stdout + r.stderr).lower() or "login" in (r.stdout + r.stderr).lower()):
        pytest.skip("Claude Code subscription login refused -p through the proxy: " + (r.stdout + r.stderr)[:300])
    assert r.returncode == 0, r.stdout + r.stderr
    _assert_clean(data, "claude-code")


@pytest.mark.skipif(not shutil.which("codex"), reason="codex not installed")
def test_codex(proxy, tmp_path):
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("Codex custom provider needs OPENAI_API_KEY; ChatGPT login does not route through a base_url")
    port, data, _ = proxy
    ws = _repo(tmp_path)
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text(
        f'model_provider = "hermie"\n[model_providers.hermie]\nname = "hermie"\n'
        f'base_url = "http://127.0.0.1:{port}/openai/v1"\nenv_key = "OPENAI_API_KEY"\nwire_api = "responses"\n')
    env = _env(CODEX_HOME=str(home / ".codex"))
    r = subprocess.run(["codex", "exec", "--full-auto", "--skip-git-repo-check", TASK], cwd=ws, env=env,
                       capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout + r.stderr
    _assert_clean(data, "codex")


@pytest.mark.skipif(not shutil.which("gemini"), reason="gemini not installed")
def test_gemini_cli(proxy, tmp_path):
    port, data, _ = proxy
    ws = _repo(tmp_path)
    env = _env(GOOGLE_GEMINI_BASE_URL=f"http://127.0.0.1:{port}/gemini")
    r = subprocess.run(["gemini", "-p", TASK, "--yolo"], cwd=ws, env=env, capture_output=True, text=True, timeout=600)
    print("gemini rc", r.returncode, "stdout:", r.stdout[-800:], "stderr:", r.stderr[-800:])
    if not _bodies(data) and not os.environ.get("GEMINI_API_KEY"):
        pytest.skip("Gemini CLI with Google login bypasses GOOGLE_GEMINI_BASE_URL (Code Assist endpoint); API-key auth only")
    assert r.returncode == 0, r.stdout + r.stderr
    _assert_clean(data, "gemini-cli")


@pytest.mark.skipif(not shutil.which("aider"), reason="aider not installed")
def test_aider(proxy, tmp_path):
    port, data, _ = proxy
    ws = _repo(tmp_path)
    env = _env(ANTHROPIC_BASE_URL=f"http://127.0.0.1:{port}/anthropic")
    r = subprocess.run(["aider", "--yes", "--no-git", "--message", TASK, "customers.csv", ".env"], cwd=ws, env=env,
                       capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout + r.stderr
    _assert_clean(data, "aider")
