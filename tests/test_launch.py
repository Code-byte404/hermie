"""`hermie claude`: one command that makes sure a proxy runs, then opens Claude Code through it with the menu, the
status line and the hooks. A tiny fake serve (writes serve.json, listens on a port) stands in for the real one, and
`call` stands in for Claude Code, so no language model is loaded and nothing reaches the network."""
import json
import os
import shlex
import socket
import subprocess
import sys
import textwrap
import time

import pytest

from hermie import launch
from hermie.config import Config

FAKE_SERVE = textwrap.dedent("""
    import json, os, signal, socket, sys, time
    data = sys.argv[1]
    s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen()
    port = s.getsockname()[1]
    path = os.path.join(data, "serve.json")
    def bye(*_):
        try: os.unlink(path)
        except OSError: pass
        sys.exit(0)
    signal.signal(signal.SIGINT, bye)   # like uvicorn: only an interrupt lets `hermie serve` remove serve.json
    with open(path, "w") as f:
        json.dump({"pid": os.getpid(), "host": "127.0.0.1", "port": port, "started": "x"}, f)
    while True: time.sleep(0.1)
""")


@pytest.fixture
def cfg(tmp_path):
    return Config(data_dir=tmp_path)


def _fake_serve(tmp_path, data_dir=None):
    script = tmp_path / "fake_serve.py"
    script.write_text(FAKE_SERVE)
    return [sys.executable, str(script), str(data_dir or tmp_path)]


def _failing_serve(tmp_path):
    script = tmp_path / "bad_serve.py"
    script.write_text("import sys; print('port 8787 is in use'); sys.exit(1)")
    return [sys.executable, str(script)]


class Claude:
    """Records how Claude Code was started and what it saw of the proxy while it ran."""

    def __init__(self, cfg, code=0):
        self.cfg, self.code, self.calls = cfg, code, []

    def __call__(self, argv, env):
        self.calls.append({"argv": argv, "env": env, "serving": launch.claude_code.serving(self.cfg)})
        return self.code


def _settings(call) -> dict:
    argv = call["argv"]
    return json.loads(argv[argv.index("--settings") + 1])


def _run(cfg, tmp_path, claude, args=(), serve=None, **kw):
    return launch.run(cfg, list(args), serve_cmd=serve or _fake_serve(tmp_path), call=claude,
                      which=lambda name: "/usr/local/bin/claude", out=open(os.devnull, "w"),
                      settings_paths=[], wait_s=10, **kw)


def test_starts_a_proxy_and_opens_claude_through_it(cfg, tmp_path):
    claude = Claude(cfg)
    assert _run(cfg, tmp_path, claude, ["-p", "hi there", "--model", "opus"]) == 0
    call = claude.calls[0]
    info = call["serving"]
    assert info is not None, "the proxy was up while Claude Code ran"
    assert call["env"]["ANTHROPIC_BASE_URL"] == f"http://127.0.0.1:{info['port']}/anthropic"
    argv = call["argv"]
    assert argv[0] == "/usr/local/bin/claude"
    assert argv[argv.index("--plugin-dir") + 1] == str(launch.MENU_DIR)
    assert argv[-4:] == ["-p", "hi there", "--model", "opus"], "the user's own arguments go last, unchanged"
    s = _settings(call)
    assert set(s["hooks"]) == {"UserPromptSubmit", "PostToolUse", "Stop"}
    assert shlex.split(s["statusLine"]["command"])[-1] == "status"


def test_the_menu_ships_inside_the_package():
    assert (launch.MENU_DIR / ".claude-plugin" / "plugin.json").is_file()
    assert (launch.MENU_DIR / "hooks" / "register.tsx").is_file()


def test_the_proxy_it_started_stops_when_claude_exits(cfg, tmp_path):
    claude = Claude(cfg)
    _run(cfg, tmp_path, claude)
    pid = claude.calls[0]["serving"]["pid"]
    assert launch.claude_code.serving(cfg) is None
    assert not (cfg.data_dir / "serve.json").exists(), "stopped cleanly, not killed"
    with pytest.raises(ProcessLookupError):
        for _ in range(50):
            os.kill(pid, 0)
            time.sleep(0.1)


def test_a_running_proxy_is_reused_and_never_stopped(cfg, tmp_path):
    own = subprocess.Popen(_fake_serve(tmp_path))
    try:
        for _ in range(100):
            if launch.claude_code.serving(cfg):
                break
            time.sleep(0.05)
        claude = Claude(cfg)
        boom = ["false"]   # a second serve must not be started
        assert _run(cfg, tmp_path, claude, serve=boom) == 0
        assert claude.calls[0]["serving"]["pid"] == own.pid
        assert launch.claude_code.serving(cfg)["pid"] == own.pid, "a proxy you started yourself keeps running"
    finally:
        own.terminate()
        own.wait()


def test_only_the_last_window_stops_the_proxy(cfg, tmp_path):
    seen = {}

    def first(argv, env):
        # a second `hermie claude` opens and closes while the first is still running
        seen["inner"] = _run(cfg, tmp_path, Claude(cfg))
        seen["after_inner"] = launch.claude_code.serving(cfg)
        return 0

    _run(cfg, tmp_path, first)
    assert seen["inner"] == 0
    assert seen["after_inner"] is not None, "the first window still needs the proxy"
    assert launch.claude_code.serving(cfg) is None


def test_a_proxy_that_fails_to_start_never_opens_claude(cfg, tmp_path):
    claude = Claude(cfg)
    err = tmp_path / "err.txt"
    with open(err, "w") as out:
        code = launch.run(cfg, [], serve_cmd=_failing_serve(tmp_path), call=claude,
                          which=lambda name: "/usr/local/bin/claude", out=out, settings_paths=[])
    assert code != 0
    assert claude.calls == [], "Claude Code is never opened without the proxy"
    text = err.read_text()
    assert "port 8787 is in use" in text, "the serve log's tail is shown"
    assert str(cfg.data_dir / "serve.log") in text


def test_claude_not_installed(cfg, tmp_path):
    claude = Claude(cfg)
    code = launch.run(cfg, [], serve_cmd=_fake_serve(tmp_path), call=claude, which=lambda name: None,
                      out=open(os.devnull, "w"), settings_paths=[])
    assert code == 127
    assert claude.calls == []
    assert launch.claude_code.serving(cfg) is None, "no proxy is left behind"


def test_claudes_exit_code_is_returned(cfg, tmp_path):
    assert _run(cfg, tmp_path, Claude(cfg, code=3)) == 3


def test_the_menu_and_hooks_find_this_hermie(cfg, tmp_path):
    claude = Claude(cfg)
    _run(cfg, tmp_path, claude)
    env = claude.calls[0]["env"]
    shim = cfg.data_dir / "bin" / "hermie"
    assert env["PATH"].split(os.pathsep)[0] == str(shim.parent)
    out = subprocess.run([str(shim), "status", "--json"], capture_output=True, text=True,
                         env={**os.environ, "HERMIE_DATA_DIR": str(cfg.data_dir)})
    assert out.returncode == 0 and "hidden" in json.loads(out.stdout)
    hook = shlex.split(_settings(claude.calls[0])["hooks"]["Stop"][0]["hooks"][0]["command"])
    assert hook[:3] == [sys.executable, "-m", "hermie.cli"]


def test_a_custom_data_dir_reaches_the_menu_and_the_hooks(tmp_path):
    cfg = Config(data_dir=tmp_path / "elsewhere")
    cfg.data_dir.mkdir()
    claude = Claude(cfg)
    _run(cfg, tmp_path, claude, serve=_fake_serve(tmp_path, cfg.data_dir), data_dir_given=True)
    call = claude.calls[0]
    assert call["env"]["HERMIE_DATA_DIR"] == str(cfg.data_dir)
    hook = shlex.split(_settings(call)["hooks"]["Stop"][0]["hooks"][0]["command"])
    assert hook[-2:] == ["--data-dir", str(cfg.data_dir)]


def test_installed_hooks_are_not_added_twice(cfg, tmp_path):
    from hermie.claude_code import install
    settings = tmp_path / "settings.local.json"
    install(settings, [sys.executable, "-m", "hermie.cli"])
    claude = Claude(cfg)
    launch.run(cfg, [], serve_cmd=_fake_serve(tmp_path), call=claude, which=lambda name: "/x/claude",
               out=open(os.devnull, "w"), settings_paths=[settings])
    argv = claude.calls[0]["argv"]
    assert "--settings" not in argv, "hermie install-hooks already added them: no second summary per turn"


def test_cli_passes_everything_after_claude_through(monkeypatch, tmp_path):
    from hermie import cli
    got = {}
    monkeypatch.setattr(launch, "run", lambda config, args, **kw: got.update(args=args, kw=kw) or 0)
    assert cli.main(["claude", "--data-dir", str(tmp_path), "-p", "hello", "--model", "opus"]) == 0
    assert got["args"] == ["-p", "hello", "--model", "opus"]
    assert got["kw"]["data_dir_given"] is True
    assert got["kw"]["serve_cmd"][-2:] == ["--data-dir", str(tmp_path)]
