import json
from datetime import datetime, timezone
from hermie import cli
from hermie.config import Config
from hermie.proxy.receipt import Receipt, ReceiptLine, BodyStore
from hermie.gate.redact import MappingStore
from hermie.proxy.approvals import AllowStore, PendingItem

def _seed(tmp_path):
    cfg = Config(data_dir=tmp_path)
    Receipt(cfg).write(ReceiptLine(at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), id="a" * 12, client="claude-code", upstream="anthropic", model="m", stream=True,
        scanned_bytes=1800, replaced={"PHONE_NUMBER": 1, "SECRET": 2}, withheld=["9c21"], held=None, approved_by=None, unrestored=0,
        judge_calls=1, judge_ms=900, status=200, upstream_error=None, mode="enforce",
        new_parts=[{"origin": "user", "tool": None, "size": 80, "entities": ["PHONE_NUMBER"]}, {"origin": "tool", "tool": "Read", "size": 812, "entities": []}]))
    BodyStore(cfg).put("a" * 12, json.dumps({"messages": [{"role": "user", "content": "fix <PHONE_NUMBER_1>"}]}).encode())
    return cfg

def test_tail_once_prints_new_parts_and_allow_hint(tmp_path, capsys):
    _seed(tmp_path)
    assert cli.main(["tail", "--once", "--data-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "claude-code" in out and "PHONE_NUMBER" in out and "hermie allow 9c21" in out and "Read" in out

def test_show_highlights_placeholders(tmp_path, capsys):
    _seed(tmp_path); cli.main(["show", "a" * 12, "--data-dir", str(tmp_path)])
    assert "<PHONE_NUMBER_1>" in capsys.readouterr().out

def test_allow_and_stats_and_forget(tmp_path, capsys, monkeypatch):
    cfg = _seed(tmp_path)
    AllowStore(tmp_path).register(PendingItem("9c21" + "0" * 60, "tool result", "judge", 10, ""))
    assert cli.main(["allow", "9c21", "--data-dir", str(tmp_path)]) == 0
    assert json.loads((tmp_path / "allowed.jsonl").read_text())["id"] == "9c21" + "0" * 60
    cli.main(["stats", "--data-dir", str(tmp_path)]); assert "SECRET" in capsys.readouterr().out
    MappingStore(cfg.mapping_path).add({"<X_1>": "y"}); monkeypatch.setattr("builtins.input", lambda *_: "y")
    cli.main(["forget", "--data-dir", str(tmp_path)]); assert MappingStore(cfg.mapping_path).mapping == {}

def test_serve_prints_base_urls(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: None)
    cli.main(["serve", "--port", "8799", "--data-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert "ANTHROPIC_BASE_URL=http://127.0.0.1:8799/anthropic" in out and "GOOGLE_GEMINI_BASE_URL" in out

def test_forget_eof_keeps_mapping(tmp_path, capsys, monkeypatch):
    cfg = _seed(tmp_path); MappingStore(cfg.mapping_path).add({"<X_1>": "y"})
    def eof(*_): raise EOFError
    monkeypatch.setattr("builtins.input", eof)
    assert cli.main(["forget", "--data-dir", str(tmp_path)]) == 0
    assert "kept" in capsys.readouterr().out and MappingStore(cfg.mapping_path).mapping == {"<X_1>": "y"}


def test_allow_unknown_id_exits_1_and_writes_nothing(tmp_path, capsys):
    assert cli.main(["allow", "zzzz", "--data-dir", str(tmp_path)]) == 1
    assert "hermie: unknown id" in capsys.readouterr().err and not (tmp_path / "allowed.jsonl").exists()
    AllowStore(tmp_path).register(PendingItem("ab12" + "0" * 60, "tool result", "detector_error: ValueError", 1, ""))
    assert cli.main(["allow", "ab12", "--data-dir", str(tmp_path)]) == 1
    assert "cannot be released" in capsys.readouterr().err and not (tmp_path / "allowed.jsonl").exists()


def test_forget_warning_says_numbers_are_not_reused(tmp_path, capsys):
    cli.main(["forget", "--yes", "--data-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert "never reused" in out and "until\nit is restarted" not in out


def test_hide_adds_values_to_the_mapping_and_reuses_placeholders(tmp_path, capsys):
    cfg = Config(data_dir=tmp_path)
    assert cli.main(["hide", "my-plain-token-value-123", "--data-dir", str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip() == "<SECRET_1>"
    assert MappingStore(cfg.mapping_path).mapping == {"<SECRET_1>": "my-plain-token-value-123"}
    assert cli.main(["hide", "my-plain-token-value-123", "second-value", "--as", "TOKEN", "--data-dir", str(tmp_path)]) == 0
    assert capsys.readouterr().out.split() == ["<SECRET_1>", "<TOKEN_1>"]
    assert MappingStore(cfg.mapping_path).mapping == {"<SECRET_1>": "my-plain-token-value-123", "<TOKEN_1>": "second-value"}


def test_hide_rejects_short_values_and_bad_entities(tmp_path, capsys):
    assert cli.main(["hide", "abc", "--data-dir", str(tmp_path)]) == 2
    assert "4 characters" in capsys.readouterr().err
    assert cli.main(["hide", "long-enough", "--as", "bad entity", "--data-dir", str(tmp_path)]) == 2
    assert not (tmp_path / "mapping.json").exists()


def test_hide_reads_stdin_when_no_value_is_given(tmp_path, capsys, monkeypatch):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("from-stdin-value\n\nanother-one\n"))
    assert cli.main(["hide", "--data-dir", str(tmp_path)]) == 0
    assert capsys.readouterr().out.split() == ["<SECRET_1>", "<SECRET_2>"]
    assert set(MappingStore(Config(data_dir=tmp_path).mapping_path).mapping.values()) == {"from-stdin-value", "another-one"}


def test_serve_hints_when_the_chinese_model_is_installed_but_off(monkeypatch, capsys, tmp_path):
    import importlib.util
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: None)
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a, **k: object() if name == "zh_core_web_sm" else real(name, *a, **k))
    cli.main(["serve", "--port", "8797", "--data-dir", str(tmp_path)])
    assert 'languages = ["en", "zh"]' in capsys.readouterr().out
