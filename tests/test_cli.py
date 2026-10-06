import json
from hermie import cli
from hermie.config import Config
from hermie.proxy.receipt import Receipt, ReceiptLine, BodyStore
from hermie.gate.redact import MappingStore

def _seed(tmp_path):
    cfg = Config(data_dir=tmp_path)
    Receipt(cfg).write(ReceiptLine(at="2026-10-06T14:02:17Z", id="a" * 12, client="claude-code", upstream="anthropic", model="m", stream=True,
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
    assert cli.main(["allow", "9c21", "--data-dir", str(tmp_path)]) == 0
    assert "9c21" in (tmp_path / "allowed.jsonl").read_text()
    cli.main(["stats", "--data-dir", str(tmp_path)]); assert "SECRET" in capsys.readouterr().out
    MappingStore(cfg.mapping_path).add({"<X_1>": "y"}); monkeypatch.setattr("builtins.input", lambda *_: "y")
    cli.main(["forget", "--data-dir", str(tmp_path)]); assert MappingStore(cfg.mapping_path).mapping == {}

def test_serve_prints_base_urls(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: None)
    cli.main(["serve", "--port", "8799", "--data-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert "ANTHROPIC_BASE_URL=http://127.0.0.1:8799/anthropic" in out and "GOOGLE_GEMINI_BASE_URL" in out
