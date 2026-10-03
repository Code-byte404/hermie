"""Connector protocol types, previews and settings."""
import json

from hermie.config import Settings
from hermie.connectors.base import ConnectorResult, Status
from hermie.connectors.preview import cap_text, preview_json, preview_table


def test_status_and_result_defaults():
    assert Status("ready").reason == ""
    r = ConnectorResult("p")
    assert r.files == () and r.ok and r.label == ""


def test_preview_json_flattens_jsonapi_rows():
    doc = {"data": [{"type": "apps", "id": "1", "attributes": {"name": "Alpha", "bundleId": "a.b"},
                     "relationships": {"x": {"links": {}}}},
                    {"type": "apps", "id": "2", "attributes": {"name": "Beta", "bundleId": "c.d"}}]}
    out = preview_json(json.dumps(doc), 4000)
    assert out.startswith("2 rows; fields: bundleId, id, name")
    assert '"name": "Alpha"' in out and "relationships" not in out


def test_preview_json_non_json_and_cap():
    assert preview_json("not json", 100) == "not json"
    long = preview_json(json.dumps([{"k": "v" * 50}] * 100), 300)
    assert len(long) < 400 and "truncated" in long


def test_preview_table_counts_rows():
    out = preview_table("a\tb\n1\t2\n3\t4\n", 1000)
    assert out.startswith("2 rows; columns: a\tb") and "3\t4" in out


def test_cap_text():
    assert cap_text("abc", 10) == "abc"
    assert cap_text("x" * 20, 10).startswith("x" * 10) and "10 more chars" in cap_text("x" * 20, 10)


def test_connector_settings(monkeypatch, tmp_path):
    monkeypatch.delenv("CONNECTORS", raising=False)
    s = Settings(data_dir=tmp_path)
    assert s.connectors is None                      # None = auto (asc if installed)
    assert s.connector_timeout == 180 and s.connector_preview_chars == 4000 and s.connector_data_keep_days == 7
    assert s.connector_rooms_dir == tmp_path / "connectors" / "rooms"
    assert s.connector_log_path == tmp_path / "connectors.jsonl"
    monkeypatch.setenv("CONNECTORS", "asc, other")
    assert Settings(data_dir=tmp_path).connectors == ["asc", "other"]
    monkeypatch.setenv("CONNECTORS", "")
    assert Settings(data_dir=tmp_path).connectors == []
