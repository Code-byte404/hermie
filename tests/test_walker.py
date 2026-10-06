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
    def is_allowed(self, h, reason=""): return h[:4] in self.ids
    def assign(self, h): return h[:4]

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


# --- final fix wave ---

class RaisingAnalyzer:
    """Fails on the tool result only (as spaCy did on texts over 1,000,000 characters)."""
    def analyze(self, text, **kw):
        if "card" in text: raise RuntimeError("analyzer down")
        return []


def _tool_result_body(text):
    return {"model": "m", "messages": [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"path": "a.txt"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": text}]}]}


def test_detector_error_is_withheld_even_when_allowed(tmp_path):
    """C2(b)/(c): a result that could not be scanned is never released raw: not by an allowed id, not by the session."""
    from hermie.proxy.approvals import AllowStore, Approvals, PendingItem, SessionAllow
    cfg = Config(data_dir=tmp_path)
    gate = Gate(cfg, analyzer=RaisingAnalyzer(), store=MappingStore(cfg.mapping_path))
    text = "card 4242424242424242 phone 555-010-0199"
    first = walk_request(_tool_result_body(text), gate, Allow(), cfg)
    d = next(d for d in first.decisions if d.kind == "withhold" and d.reason.startswith("detector_error"))
    store = AllowStore(tmp_path)
    store.register(PendingItem(d.hash, "tool result", d.reason, 1, ""))
    store.allow(d.hash)                                   # even a stored full-hash allow
    session = Approvals(store, SessionAllow())
    session.session.all = True
    for approvals in (Allow([d.id]), session):
        out = walk_request(_tool_result_body(text), gate, approvals, cfg)
        dumped = json.dumps(out.body)
        assert "4242424242424242" not in dumped and "555-010-0199" not in dumped
        assert "could not be scanned" in out.body["messages"][2]["content"][0]["content"]
        assert not [x for x in out.decisions if x.kind == "pass"]


def test_payload_keys_and_long_numbers_are_scanned_and_restore(tmp_path, analyzer):
    """C3: keys and numbers inside tool payloads; protocol keys and numbers outside payloads untouched."""
    from hermie.proxy.stream import restore_json
    cfg, gate = _gate(tmp_path, analyzer)
    gem = json.loads((FIX / "gemini.json").read_text())
    gem["generationConfig"] = {"maxOutputTokens": 4096, "seed": 1234567}
    response = {"card": 4242424242424242, "alice@example.com": "vip", "555-010-0199": {"tier": "gold"}, "count": 3}
    gem["contents"][2]["parts"][0]["functionResponse"]["response"] = response
    out = walk_request(json.loads(json.dumps(gem)), gate, Allow(), cfg)
    resp = out.body["contents"][2]["parts"][0]["functionResponse"]["response"]
    dumped = json.dumps(resp)
    for real in ("4242424242424242", "alice@example.com", "555-010-0199"):
        assert real not in dumped
    ph = {v: k for k, v in gate.mapping.items()}
    assert resp["card"] == ph["4242424242424242"] and resp[ph["alice@example.com"]] == "vip" and resp["count"] == 3
    assert list(resp) == ["card", ph["alice@example.com"], ph["555-010-0199"], "count"]
    assert out.body["generationConfig"] == {"maxOutputTokens": 4096, "seed": 1234567}
    back, missing = restore_json(resp, gate.restore)
    assert missing == 0
    assert back == response | {"card": "4242424242424242"}   # a number comes back as its digits (a string)

    ant = json.loads((FIX / "anthropic.json").read_text())
    ant["max_tokens"] = 4096
    ant["messages"][1]["content"][0]["input"] = {"bob@example.com": "to"}
    out = walk_request(ant, gate, Allow(), cfg)
    assert out.body["messages"][1]["content"][0]["input"] == {{v: k for k, v in gate.mapping.items()}["bob@example.com"]: "to"}
    assert out.body["max_tokens"] == 4096


def test_withheld_note_shows_the_store_id_on_a_collision(tmp_path, analyzer):
    """I1: the note's id is the allow store's id, which grows on a collision."""
    import hashlib
    from hermie.proxy.approvals import AllowStore, Approvals, PendingItem, SessionAllow
    cfg, gate = _gate(tmp_path, analyzer, judge=SensitiveToolJudge())
    text = ".env\nAWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"
    h = hashlib.sha256(text.encode()).hexdigest()
    store = AllowStore(tmp_path)
    store.register(PendingItem(h[:4] + "f" * 60 if h[4] != "f" else h[:4] + "e" * 60, "tool result", "judge", 1, ""))
    out = walk_request(_tool_result_body(text), gate, Approvals(store, SessionAllow()), cfg)
    note = out.body["messages"][2]["content"][0]["content"]
    assert f"(id {h[:6]})" in note and f'hermie allow {h[:6]}"' in note


def test_user_text_with_an_encoded_blob_is_held_for_smuggling(tmp_path, analyzer):
    class Never:
        def is_sensitive(self, text): return False
    cfg, gate = _gate(tmp_path, analyzer, judge=Never())
    body = {"model": "m", "messages": [{"role": "user", "content": [
        {"type": "text", "text": "store this: UEsDBBQAAAAIAGx3R1kAAAAAAAAAAAAAAAAJAAAAZGF0YS5jc3ZLzs8tKEotLs5MT0ksSQUA"}]}]}
    out = walk_request(body, gate, Allow(), cfg)
    assert out.held is not None and out.held.reason.startswith("smuggling: possible base64")
    long_prose = {"model": "m", "messages": [{"role": "user", "content": "Please refactor the module. " * 40}]}
    assert walk_request(long_prose, gate, Allow(), cfg).held is None
