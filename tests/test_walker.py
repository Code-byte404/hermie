import json, pytest
from pathlib import Path
from hermie.config import Config
from hermie.gate.gate import Gate
from hermie.gate.redact import MappingStore
from hermie.gate.types import Origin
from hermie.proxy.walker import walk_request, classify, SKIP_KEYS, WITHHELD_NOTE

FIX = Path(__file__).parent / "fixtures" / "requests"
FORMATS = ["anthropic", "openai_chat", "openai_responses", "gemini"]

class Allow:
    def __init__(self, ids=()): self.ids = set(ids)
    def is_allowed(self, h): return h[:4] in self.ids

class SensitiveToolJudge:
    def is_sensitive(self, text): return "AKIA" in text or "<SECRET_" in text

def _gate(tmp_path, analyzer, judge=None, **kw):
    cfg = Config(data_dir=tmp_path, judge="ollama:x" if judge else None, **kw)
    return cfg, Gate(cfg, analyzer=analyzer, judge=judge, store=MappingStore(cfg.mapping_path))

@pytest.mark.parametrize("fmt", FORMATS)
def test_no_real_value_reaches_the_body(fmt, tmp_path, analyzer):
    cfg, gate = _gate(tmp_path, analyzer)
    body = json.loads((FIX / f"{fmt}.json").read_text())
    out = walk_request(body, gate, Allow(), cfg)
    dumped = json.dumps(out.body)
    for real in ("555-010-0199", "me@example.com", "AKIAIOSFODNN7EXAMPLE"):
        assert real not in dumped
    assert out.counts["PHONE_NUMBER"] == 1 and out.counts["SECRET"] >= 1
    assert out.body["model"] == body["model"]            # skip keys untouched

@pytest.mark.parametrize("fmt", FORMATS)
def test_origins(fmt):
    body = json.loads((FIX / f"{fmt}.json").read_text())
    seen = {classify(p, body) for p in _string_paths(body)}
    assert {Origin.USER, Origin.TOOL, Origin.ASSISTANT} <= seen

def test_tool_result_withheld_with_id_and_released_after_allow(tmp_path, analyzer):
    cfg, gate = _gate(tmp_path, analyzer, judge=SensitiveToolJudge())
    body = json.loads((FIX / "anthropic.json").read_text())
    out = walk_request(body, gate, Allow(), cfg)
    note = out.body["messages"][2]["content"][0]["content"]
    assert note.startswith("[hermie withheld this tool result (id ") and "hermie allow" in note
    d = [d for d in out.decisions if d.kind == "withhold"][0]
    out2 = walk_request(json.loads((FIX / "anthropic.json").read_text()), gate, Allow([d.id]), cfg)
    assert "<SECRET_1>" in out2.body["messages"][2]["content"][0]["content"]   # released: pattern-redacted, not raw

def test_user_text_flagged_is_held_not_rewritten(tmp_path, analyzer):
    class J:  # flags the user turn only
        def is_sensitive(self, text): return "555" in text or "<PHONE_NUMBER_" in text
    cfg, gate = _gate(tmp_path, analyzer, judge=J())
    out = walk_request(json.loads((FIX / "anthropic.json").read_text()), gate, Allow(), cfg)
    assert out.held is not None and out.held.kind == "hold"

def test_images_withheld_by_default_and_passed_on_config(tmp_path, analyzer):
    cfg, gate = _gate(tmp_path, analyzer)
    out = walk_request(json.loads((FIX / "anthropic.json").read_text()), gate, Allow(), cfg)
    blk = out.body["messages"][0]["content"][1]
    assert blk["type"] == "text" and "withheld an image" in blk["text"]
    cfg2, gate2 = _gate(tmp_path / "b", analyzer, images="pass")
    out2 = walk_request(json.loads((FIX / "anthropic.json").read_text()), gate2, Allow(), cfg2)
    assert out2.body["messages"][0]["content"][1]["type"] == "image"

def test_observe_mode_changes_nothing_but_records(tmp_path, analyzer):
    cfg, gate = _gate(tmp_path, analyzer, mode="observe")
    body = json.loads((FIX / "anthropic.json").read_text())
    out = walk_request(json.loads(json.dumps(body)), gate, Allow(), cfg)
    assert out.body == body and out.counts["PHONE_NUMBER"] == 1

def test_path_hint_from_tool_result_first_line(tmp_path, analyzer):
    cfg, gate = _gate(tmp_path, analyzer, allow_paths=(".env",))
    out = walk_request(json.loads((FIX / "anthropic.json").read_text()), gate, Allow(), cfg)
    assert "AKIAIOSFODNN7EXAMPLE" in out.body["messages"][2]["content"][0]["content"]

def _string_paths(node, path=()):
    if isinstance(node, dict):
        for k, v in node.items(): yield from _string_paths(v, path + (k,))
    elif isinstance(node, list):
        for i, v in enumerate(node): yield from _string_paths(v, path + (i,))
    elif isinstance(node, str): yield path


def test_payload_subtrees_ignore_skip_keys(tmp_path, analyzer):
    cfg, gate = _gate(tmp_path, analyzer)
    gem = json.loads((FIX / "gemini.json").read_text())
    gem["contents"][2]["parts"][0]["functionResponse"]["response"] = {"name": "Maria Gonzalez", "phone": "555-010-0199"}
    out = walk_request(gem, gate, Allow(), cfg)
    resp = out.body["contents"][2]["parts"][0]["functionResponse"]["response"]
    assert "Maria" not in resp["name"] and "555-010-0199" not in resp["phone"]
    assert out.body["model"] == gem["model"]

    ant = json.loads((FIX / "anthropic.json").read_text())
    ant["model"] = "claude-opus-5-5"
    ant["messages"][1]["content"][0]["input"] = {"name": "555-010-0199"}
    out = walk_request(ant, gate, Allow(), cfg)
    assert "555-010-0199" not in json.dumps(out.body["messages"][1])
    assert out.body["model"] == "claude-opus-5-5" and out.body["messages"][1]["content"][0]["name"] == "Read"

def test_system_role_message_is_user_and_held_when_flagged(tmp_path, analyzer):
    body = json.loads((FIX / "openai_chat.json").read_text())
    assert classify(("messages", 0, "content"), body) is Origin.USER
    body["input"] = [{"role": "developer", "content": [{"type": "input_text", "text": "x"}]}]
    assert classify(("input", 0, "content", 0, "text"), body) is Origin.USER
    class J:
        def is_sensitive(self, text): return "me@example.com" in text or "<EMAIL_ADDRESS_" in text
    cfg, gate = _gate(tmp_path, analyzer, judge=J())
    out = walk_request(json.loads((FIX / "openai_chat.json").read_text()), gate, Allow(), cfg)
    assert out.held is not None and out.held.kind == "hold"
