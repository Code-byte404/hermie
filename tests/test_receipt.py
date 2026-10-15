import dataclasses
import json
import os
import time

from hermie.config import Config
from hermie.proxy.receipt import BodyStore, Receipt, ReceiptLine, client_label


def _line(**kw):
    base = dict(at="2026-10-06T14:02:17Z", id="a" * 12, client="claude-code", upstream="anthropic", model="m", stream=True,
                scanned_bytes=10, replaced={"PHONE_NUMBER": 1}, withheld=[], held=None, approved_by=None, unrestored=0,
                judge_calls=0, judge_ms=0, status=200, upstream_error=None, mode="enforce", new_parts=[])
    return ReceiptLine(**(base | kw))


def test_receipt_appends_and_reads_back(tmp_path):
    r = Receipt(Config(data_dir=tmp_path)); r.write(_line()); r.write(_line(id="b" * 12))
    assert [l.id for l in r.iter()] == ["a" * 12, "b" * 12]
    assert oct((tmp_path / "receipt.jsonl").stat().st_mode & 0o777) == "0o600"


def test_receipt_has_no_free_text_field():
    names = {f.name for f in dataclasses.fields(ReceiptLine)}
    assert not names & {"text", "excerpt", "content", "body", "message", "reason"}
    assert "new_parts" in names


def test_receipt_iter_skips_malformed_and_filters_since(tmp_path):
    from datetime import datetime, timezone
    r = Receipt(Config(data_dir=tmp_path))
    r.write(_line(at="2026-10-06T10:00:00Z", id="a" * 12))
    with open(tmp_path / "receipt.jsonl", "a") as f:
        f.write("not json\n")
    r.write(_line(at="2026-10-06T12:00:00Z", id="b" * 12))
    since = datetime(2026, 10, 6, 11, 0, tzinfo=timezone.utc)
    assert [l.id for l in r.iter()] == ["a" * 12, "b" * 12]
    assert [l.id for l in r.iter(since=since)] == ["b" * 12]


def test_receipt_follow_tails_and_closes(tmp_path):
    r = Receipt(Config(data_dir=tmp_path)); r.write(_line())
    gen = r.follow(poll_s=0.01)
    assert next(gen).id == "a" * 12
    r.write(_line(id="c" * 12))
    assert next(gen).id == "c" * 12
    gen.close()


def test_body_store_retention(tmp_path):
    cfg = Config(data_dir=tmp_path, bodies_keep_mb=1)
    bs = BodyStore(cfg)
    for i in range(12): bs.put(f"{i:012x}", b"x" * 100_000)      # 1.2 MB into a 1 MB cap
    files = sorted((tmp_path / "outbound").glob("*.json"))
    assert len(files) <= 10 and bs.get("00000000000b") is not None and bs.get("000000000000") is None
    assert oct(files[0].stat().st_mode & 0o777) == "0o600"


def test_body_store_keeps_single_oversize_body(tmp_path):
    bs = BodyStore(Config(data_dir=tmp_path, bodies_keep_mb=1))
    bs.put("a" * 12, b"x" * 100)
    bs.put("b" * 12, b"x" * 2_000_000)
    assert bs.get("b" * 12) is not None and bs.get("a" * 12) is None


def test_body_store_disabled(tmp_path):
    assert BodyStore(Config(data_dir=tmp_path, bodies=False)).put("a" * 12, b"{}") is None
    assert not (tmp_path / "outbound").exists()


def test_body_store_rejects_bad_ids(tmp_path):
    bs = BodyStore(Config(data_dir=tmp_path))
    bs.put("a" * 12, b"{}")
    assert bs.get("a" * 12) == b"{}"
    assert bs.get("../receipt") is None and bs.get("A" * 12) is None and bs.get("a" * 11) is None


def test_client_label():
    assert client_label("claude-cli/2.1.291 (external, cli)") == "claude-code"
    assert client_label("codex_cli_rs/0.147.0") == "codex"
    assert client_label("GeminiCLI/0.44.1") == "gemini-cli"
    assert client_label("litellm/1.0") == "aider"
    assert client_label("curl/8.1 extra") == "curl/8.1"
    assert client_label(None) == "unknown"
    assert client_label("") == "unknown"


def test_receipt_write_strips_stray_keys_and_coerces(tmp_path):
    r = Receipt(Config(data_dir=tmp_path))
    r.write(_line(new_parts=[{"origin": "user", "tool": None, "size": "42", "entities": ["PHONE_NUMBER"],
                              "text": "call 555-0100"}],
                  replaced={"PHONE_NUMBER": "2", "EMAIL": "bob@example.com"}, withheld=["h1", 7]))
    raw = (tmp_path / "receipt.jsonl").read_text()
    assert "555-0100" not in raw and "bob@example.com" not in raw
    [got] = list(r.iter())
    assert got.new_parts == [{"origin": "user", "tool": None, "size": 42, "entities": ["PHONE_NUMBER"]}]
    assert got.replaced == {"PHONE_NUMBER": 2} and got.withheld == ["h1", "7"]


def test_receipt_line_without_restored_field_still_parses(tmp_path):
    """Older lines have no `restored`; they read back with 0."""
    r = Receipt(Config(data_dir=tmp_path))
    old = {f.name: getattr(_line(), f.name) for f in dataclasses.fields(ReceiptLine) if f.name != "restored"}
    with open(tmp_path / "receipt.jsonl", "a") as f:
        f.write(json.dumps(old) + "\n")
    r.write(_line(restored=3))
    lines = list(r.iter())
    assert [l.restored for l in lines] == [0, 3]


def test_body_store_put_never_raises_on_unwritable_dir(tmp_path):
    (tmp_path / "outbound").write_text("not a directory")
    assert BodyStore(Config(data_dir=tmp_path)).put("a" * 12, b"{}") is None


def test_body_store_writes_whole_body_on_short_writes(tmp_path, monkeypatch):
    real = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: real(fd, bytes(data[:7])))
    bs = BodyStore(Config(data_dir=tmp_path))
    body = json.dumps({"text": "x" * 1000}).encode()
    bs.put("a" * 12, body)
    assert bs.get("a" * 12) == body


def test_body_store_does_not_rescan_the_directory_under_the_cap(tmp_path, monkeypatch):
    bs = BodyStore(Config(data_dir=tmp_path, bodies_keep_mb=1))
    bs.put("f" * 12, b"{}")                       # first put may read the directory once
    calls = []
    real = type(bs.dir).glob
    monkeypatch.setattr(type(bs.dir), "glob", lambda self, pat: calls.append(pat) or real(self, pat))
    for i in range(50):
        bs.put(f"{i:012x}", b"x" * 1000)
    assert calls == []


def test_receipt_new_fields_keep_names_only(tmp_path):
    r = Receipt(Config(data_dir=tmp_path))
    r.write(_line(placeholders=["<PERSON_1>", "Alice Chen", "<SECRET_1>"],
                  withheld_items=[{"id": "9c21", "tool": "Read", "file": "/home/a/customers.csv", "why": "judged private",
                                   "text": "555-010-0199"},
                                  {"id": "9c22", "tool": None, "file": None, "why": "possible hex data: 3f9a1c"}]))
    raw = (tmp_path / "receipt.jsonl").read_text()
    assert "Alice Chen" not in raw and "555-010-0199" not in raw and "3f9a1c" not in raw and "/home/a" not in raw
    [got] = list(r.iter())
    assert got.placeholders == ["<PERSON_1>", "<SECRET_1>"]
    assert got.withheld_items == [{"id": "9c21", "tool": "Read", "file": "customers.csv", "why": "judged private"},
                                  {"id": "9c22", "tool": None, "file": None, "why": "held for review"}]


def test_receipt_line_without_new_fields_still_parses(tmp_path):
    r = Receipt(Config(data_dir=tmp_path))
    old = {f.name: getattr(_line(), f.name) for f in dataclasses.fields(ReceiptLine)
           if f.name not in ("placeholders", "withheld_items")}
    with open(tmp_path / "receipt.jsonl", "a") as f:
        f.write(json.dumps(old) + "\n")
    [got] = list(r.iter())
    assert got.placeholders == [] and got.withheld_items == []
