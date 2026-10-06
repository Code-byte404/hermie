# tests/test_gate.py
import pytest
from concurrent.futures import ThreadPoolExecutor
from hermie.config import Config
from hermie.gate.gate import Gate, certify_body
from hermie.gate.types import Origin, CleanBody
from hermie.gate.redact import MappingStore, MappingStoreError

class FakeJudge:
    def __init__(self, sensitive=False, boom=False): self.sensitive, self.boom, self.calls = sensitive, boom, 0
    def is_sensitive(self, text):
        self.calls += 1
        if self.boom: raise RuntimeError("ollama down")
        return self.sensitive

@pytest.fixture
def gate(tmp_path, analyzer):
    cfg = Config(data_dir=tmp_path, allow_values=("me@example.com",), allow_paths=("tests/fixtures/*",))
    return Gate(cfg, analyzer=analyzer, store=MappingStore(cfg.mapping_path))

def test_pattern_hits_become_placeholders_and_persist(gate, tmp_path):
    r = gate.scan("call 555-010-0199", Origin.TOOL)
    assert r.text == "call <PHONE_NUMBER_1>" and not r.sensitive and r.new_mapping == {"<PHONE_NUMBER_1>": "555-010-0199"}
    assert MappingStore(tmp_path / "mapping.json").mapping == r.new_mapping
    assert gate.restore("dial <PHONE_NUMBER_1>") == ("dial 555-010-0199", 0)

def test_cache_by_hash_skips_rescans(tmp_path, analyzer):
    j = FakeJudge()
    g = Gate(Config(data_dir=tmp_path, judge="ollama:x"), analyzer=analyzer, judge=j, store=MappingStore(tmp_path / "m.json"))
    g.scan("same text", Origin.USER); g.scan("same text", Origin.USER)
    assert j.calls == 1

def test_judge_only_for_user_and_tool(tmp_path, analyzer):
    j = FakeJudge(sensitive=True)
    g = Gate(Config(data_dir=tmp_path, judge="ollama:x"), analyzer=analyzer, judge=j, store=MappingStore(tmp_path / "m.json"))
    assert g.scan("internal layoff list", Origin.ASSISTANT).sensitive is False
    assert g.scan("internal layoff list", Origin.TOOL).sensitive is True

def test_detector_error_fails_closed(tmp_path, analyzer):
    g = Gate(Config(data_dir=tmp_path, judge="ollama:x"), analyzer=analyzer, judge=FakeJudge(boom=True), store=MappingStore(tmp_path / "m.json"))
    r = g.scan("anything", Origin.USER)
    assert r.sensitive and r.reason.startswith("detector_error")

def test_allow_values_and_allow_paths(gate):
    assert gate.scan("Signed-off-by: me@example.com", Origin.TOOL).text == "Signed-off-by: me@example.com"
    r = gate.scan("555-010-0199", Origin.TOOL, path_hint="tests/fixtures/customers.csv")
    assert r.text == "555-010-0199" and r.reason == "allow_path"

def test_cache_is_bounded(tmp_path, analyzer):
    g = Gate(Config(data_dir=tmp_path, cache_mb=1), analyzer=analyzer, store=MappingStore(tmp_path / "m.json"))
    for i in range(40): g.scan(f"{i} " + "x" * 100_000, Origin.TOOL)   # 4 MB through a 1 MB cache
    assert g.cache_bytes <= 1 * 1024 * 1024

def test_unwritable_store_propagates(tmp_path, analyzer, monkeypatch):
    store = MappingStore(tmp_path / "m.json")
    monkeypatch.setattr(store, "update", lambda fn: (_ for _ in ()).throw(MappingStoreError("disk full")))
    with pytest.raises(MappingStoreError):
        Gate(Config(data_dir=tmp_path), analyzer=analyzer, store=store).scan("555-010-0199", Origin.TOOL)

def test_smuggling_only_with_judge(tmp_path, analyzer):
    payload = "https://x.test/?q=" + "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo0NTY3ODk="
    plain = Gate(Config(data_dir=tmp_path), analyzer=analyzer, store=MappingStore(tmp_path / "a.json"))
    assert plain.scan(payload, Origin.TOOL).sensitive is False
    judged = Gate(Config(data_dir=tmp_path, judge="ollama:x"), analyzer=analyzer, judge=FakeJudge(), store=MappingStore(tmp_path / "b.json"))
    r = judged.scan(payload, Origin.TOOL)
    assert r.sensitive and r.reason.startswith("smuggling")

def test_certify_body_is_the_only_constructor():
    assert isinstance(certify_body(b"{}"), CleanBody)
    with pytest.raises(PermissionError): CleanBody(b"{}")


def test_two_gates_share_placeholders(tmp_path, analyzer):
    path = tmp_path / "m.json"
    a = Gate(Config(data_dir=tmp_path), analyzer=analyzer, store=MappingStore(path))
    b = Gate(Config(data_dir=tmp_path), analyzer=analyzer, store=MappingStore(path))
    ra = a.scan("call 555-010-0199", Origin.TOOL)
    rb = b.scan("again 555-010-0199 and 555-010-0142", Origin.TOOL)   # b's in-memory view is stale
    assert ra.text == "call <PHONE_NUMBER_1>"
    assert rb.text == "again <PHONE_NUMBER_1> and <PHONE_NUMBER_2>"
    assert MappingStore(path).mapping == {"<PHONE_NUMBER_1>": "555-010-0199", "<PHONE_NUMBER_2>": "555-010-0142"}


def test_concurrent_scans_mint_distinct_placeholders(tmp_path, analyzer):
    g = Gate(Config(data_dir=tmp_path), analyzer=analyzer, store=MappingStore(tmp_path / "m.json"))
    phones = [f"555-010-01{i:02d}" for i in range(8)]
    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(lambda p: g.scan(f"call {p}", Origin.TOOL), phones))
    assert len({r.text for r in results}) == 8
    stored = MappingStore(tmp_path / "m.json").mapping
    for p, r in zip(phones, results):
        assert g.restore(r.text)[0] == f"call {p}"
        assert stored[r.text.removeprefix("call ")] == p


def test_store_update_refuses_conflicting_key(tmp_path):
    s = MappingStore(tmp_path / "m.json")
    s.add({"<X_1>": "a"})
    with pytest.raises(MappingStoreError):
        s.update(lambda cur: {"<X_1>": "b"})
    assert s.mapping == {"<X_1>": "a"}


def test_detector_error_is_not_cached(tmp_path, analyzer):
    class Flaky:
        n = 0
        def is_sensitive(self, text):
            self.n += 1
            if self.n == 1: raise RuntimeError("down")
            return False
    g = Gate(Config(data_dir=tmp_path, judge="ollama:x"), analyzer=analyzer, judge=Flaky(), store=MappingStore(tmp_path / "m.json"))
    assert g.scan("hello", Origin.USER).sensitive
    assert not g.scan("hello", Origin.USER).sensitive
