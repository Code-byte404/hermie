"""Claude Code integration: `hermie status` (status line), `hermie hook` (hook events), `hermie install-hooks`."""
import json
import os
import socket
from datetime import datetime, timedelta, timezone

from hermie import cli
from hermie.claude_code import handle_hook, install, status_json, status_text, uninstall
from hermie.config import Config
from hermie.gate.redact import MappingStore
from hermie.proxy.receipt import Receipt, ReceiptLine


def _stamp(delta_s=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_s)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _line(at=None, **kw):
    base = dict(at=at or _stamp(), id="a" * 12, client="claude-code", upstream="anthropic", model="m", stream=True,
                scanned_bytes=10, replaced={}, withheld=[], held=None, approved_by=None, unrestored=0, judge_calls=0,
                judge_ms=0, status=200, upstream_error=None, mode="enforce", new_parts=[], restored=0)
    return ReceiptLine(**(base | kw))


def test_status_text_when_not_running(tmp_path):
    cfg = Config(data_dir=tmp_path, port=8799)
    cfg.serve_path.write_text(json.dumps({"pid": 2 ** 22 - 1, "host": "127.0.0.1", "port": 8799, "started": "x"}))  # stale
    MappingStore(cfg.mapping_path).add({"<SECRET_1>": "sk-test-abc", "<EMAIL_ADDRESS_1>": "me@example.com"})
    Receipt(cfg).write(_line(replaced={"SECRET": 1, "EMAIL_ADDRESS": 2}, restored=1))
    text = status_text(cfg)
    assert "not running" in text and "2 values kept local" in text
    assert "2 EMAIL_ADDRESS, 1 SECRET replaced" in text and "1 restored" in text
    assert "sk-test-abc" not in text and "me@example.com" not in text
    assert status_json(cfg)["running"] is False and status_json(cfg)["kept"] == 2


def test_status_text_when_serving(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); s.listen(8)
        port = s.getsockname()[1]
        cfg = Config(data_dir=tmp_path, port=8787)      # the serve file wins over the configured port
        cfg.serve_path.write_text(json.dumps({"pid": os.getpid(), "host": "127.0.0.1", "port": port, "started": "x"}))
        text = status_text(cfg)
        running = status_json(cfg)["running"]
    assert f"hermie ● :{port}" in text and "0 values kept local" in text and running is True
    assert status_text(cfg).startswith("hermie ○ not running")        # the socket is closed now


def test_serve_writes_and_removes_its_file(tmp_path, monkeypatch):
    seen = {}
    def fake_run(*a, **k):
        seen.update(json.loads(Config(data_dir=tmp_path).serve_path.read_text()))
    monkeypatch.setattr("uvicorn.run", fake_run)
    assert cli.main(["serve", "--port", "8798", "--data-dir", str(tmp_path)]) == 0
    assert seen["pid"] == os.getpid() and seen["port"] == 8798 and not Config(data_dir=tmp_path).serve_path.exists()


def test_hook_prompt_marker_then_stop_summarizes_only_this_turn(tmp_path):
    cfg = Config(data_dir=tmp_path)
    Receipt(cfg).write(_line(at=_stamp(-120), replaced={"PHONE_NUMBER": 5}))     # an earlier turn
    assert handle_hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-1", "prompt": "x"}, cfg) is None
    Receipt(cfg).write(_line(at=_stamp(1), replaced={"SECRET": 1}, restored=1))
    Receipt(cfg).write(_line(at=_stamp(2), id="b" * 12))                            # a side request, nothing in it
    out = handle_hook({"hook_event_name": "Stop", "session_id": "s-1"}, cfg)
    msg = out["systemMessage"]
    assert msg.startswith("Hermie:") and "1 SECRET" in msg and "1 restored" in msg and "PHONE_NUMBER" not in msg


def test_hook_stop_is_quiet_when_nothing_was_replaced_or_restored(tmp_path):
    cfg = Config(data_dir=tmp_path)
    handle_hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-1"}, cfg)
    Receipt(cfg).write(_line(at=_stamp(1)))
    assert handle_hook({"hook_event_name": "Stop", "session_id": "s-1"}, cfg) is None


def test_hook_stop_reports_held_and_withheld(tmp_path):
    cfg = Config(data_dir=tmp_path)
    handle_hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-1"}, cfg)
    Receipt(cfg).write(_line(at=_stamp(1), withheld=["9c21"], held="ab12"))
    msg = handle_hook({"hook_event_name": "Stop", "session_id": "s-1"}, cfg)["systemMessage"]
    assert "1 held" in msg and "1 withheld" in msg and "hermie allow" in msg


def test_hook_post_tool_use_reports_restored_values_for_file_writes(tmp_path):
    cfg = Config(data_dir=tmp_path)
    handle_hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-1"}, cfg)
    Receipt(cfg).write(_line(at=_stamp(1), replaced={"SECRET": 1}, restored=1))
    ev = {"hook_event_name": "PostToolUse", "session_id": "s-1", "tool_name": "Write",
          "tool_input": {"file_path": "/tmp/proj/.env", "content": "OPENAI_API_KEY=sk-test-abc"}}
    msg = handle_hook(ev, cfg)["systemMessage"]
    assert "1 placeholder" in msg and "Write" in msg and ".env" in msg and "never left" in msg
    assert "sk-test-abc" not in msg and "/tmp/proj" not in msg
    assert handle_hook(ev | {"tool_name": "Bash", "tool_input": {"command": "ls"}}, cfg) is None


def test_hook_post_tool_use_is_quiet_without_restored_values(tmp_path):
    cfg = Config(data_dir=tmp_path)
    handle_hook({"hook_event_name": "UserPromptSubmit", "session_id": "s-1"}, cfg)
    Receipt(cfg).write(_line(at=_stamp(1), replaced={"SECRET": 1}))
    ev = {"hook_event_name": "PostToolUse", "session_id": "s-1", "tool_name": "Edit", "tool_input": {"file_path": "a.py"}}
    assert handle_hook(ev, cfg) is None


def test_hook_tolerates_bad_input(tmp_path, monkeypatch, capsys):
    cfg = Config(data_dir=tmp_path)
    assert handle_hook({}, cfg) is None
    assert handle_hook({"hook_event_name": "Stop", "session_id": "../../etc"}, cfg) is None
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert cli.main(["hook", "--data-dir", str(tmp_path)]) == 0 and capsys.readouterr().out == ""


def test_hook_cli_prints_json_for_claude_code(tmp_path, monkeypatch, capsys):
    import io
    cfg = Config(data_dir=tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "s9"})))
    assert cli.main(["hook", "--data-dir", str(tmp_path)]) == 0 and capsys.readouterr().out == ""
    Receipt(cfg).write(_line(at=_stamp(1), replaced={"SECRET": 2}))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"hook_event_name": "Stop", "session_id": "s9"})))
    assert cli.main(["hook", "--data-dir", str(tmp_path)]) == 0
    assert "2 SECRET" in json.loads(capsys.readouterr().out)["systemMessage"]


def test_install_merges_idempotently_and_uninstall_removes_only_ours(tmp_path):
    path = tmp_path / ".claude" / "settings.local.json"
    path.parent.mkdir()
    other = {"type": "command", "command": "echo other"}
    path.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]},
                                "hooks": {"Stop": [{"hooks": [other]}]}}))
    install(path, "/opt/bin/hermie")
    install(path, "/opt/bin/hermie")
    data = json.loads(path.read_text())
    assert data["permissions"] == {"allow": ["Bash(ls)"]}
    assert data["statusLine"] == {"type": "command", "command": "/opt/bin/hermie status"}
    stops = data["hooks"]["Stop"]
    assert [h["hooks"][0]["command"] for h in stops] == ["echo other", "/opt/bin/hermie hook"]
    assert data["hooks"]["UserPromptSubmit"] == [{"hooks": [{"type": "command", "command": "/opt/bin/hermie hook"}]}]
    assert data["hooks"]["PostToolUse"][0]["matcher"] == "Write|Edit|MultiEdit|NotebookEdit"
    uninstall(path)
    data = json.loads(path.read_text())
    assert data["hooks"] == {"Stop": [{"hooks": [other]}]} and "statusLine" not in data and "permissions" in data


def test_install_keeps_a_foreign_status_line(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"statusLine": {"type": "command", "command": "my-status.sh"}}))
    notes = install(path, "hermie")
    assert json.loads(path.read_text())["statusLine"]["command"] == "my-status.sh"
    assert any("my-status.sh" in n and "hermie status" in n for n in notes)


def test_install_cli_writes_project_local_settings(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["install-hooks", "--data-dir", str(tmp_path / "data")]) == 0
    data = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
    cmd = data["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert cmd.endswith(f" hook --data-dir {tmp_path / 'data'}") and data["statusLine"]["command"].endswith(f" status --data-dir {tmp_path / 'data'}")
    out = capsys.readouterr().out
    assert "settings.local.json" in out and "new Claude Code session" in out
    assert cli.main(["install-hooks", "--uninstall", "--data-dir", str(tmp_path / "data")]) == 0
    assert "hooks" not in json.loads((tmp_path / ".claude" / "settings.local.json").read_text())


def test_summary_takes_the_largest_request_per_entity_and_sums_restored():
    from hermie.claude_code import summarize
    lines = [_line(replaced={"PERSON": 16, "SECRET": 2, "EMAIL_ADDRESS": 4, "PHONE_NUMBER": 1}, restored=1),
             _line(replaced={"PERSON": 16, "SECRET": 1}, restored=2)]
    assert summarize(lines) == "16 PERSON, 4 EMAIL_ADDRESS, 2 SECRET and 1 more replaced; 3 restored"
    assert summarize([_line()]) is None
