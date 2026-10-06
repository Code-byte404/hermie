# tests/test_gate.py
import json
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
        s.update(lambda cur, counters: {"<X_1>": "b"})
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


# --- final fix wave ---

def test_known_values_map_back_to_their_placeholders(tmp_path, analyzer):
    """C1: a value restored on the way in (assistant text, tool call, tool result) maps back to its placeholder even
    where the recognizers would not see it in its new context."""
    g = Gate(Config(data_dir=tmp_path), analyzer=analyzer, store=MappingStore(tmp_path / "m.json"))
    first = g.scan("password = Hunter2Secret99 and call 555-010-0199", Origin.USER)
    assert "<SECRET_1>" in first.text and "<PHONE_NUMBER_1>" in first.text
    for origin, text in ((Origin.ASSISTANT, "mysql -pHunter2Secret99"), (Origin.TOOL, "logged in with Hunter2Secret99"),
                         (Origin.OTHER, "dial5550100199 555-010-0199x")):
        r = g.scan(text, origin)
        assert "Hunter2Secret99" not in r.text and "555-010-0199" not in r.text, (origin, r.text)
    assert g.scan("mysql -pHunter2Secret99", Origin.ASSISTANT).text == "mysql -p<SECRET_1>"
    assert set(MappingStore(tmp_path / "m.json").mapping) == {"<SECRET_1>", "<PHONE_NUMBER_1>"}


def test_known_values_reach_cached_results_and_skip_short_values(tmp_path, analyzer):
    g = Gate(Config(data_dir=tmp_path), analyzer=analyzer, store=MappingStore(tmp_path / "m.json"))
    assert g.scan("ran with Zq9rTk3Lw", Origin.TOOL).text == "ran with Zq9rTk3Lw"     # nothing known yet: cached raw
    g.scan("token = Zq9rTk3Lw", Origin.USER)                                         # now it is a known value
    assert g.scan("ran with Zq9rTk3Lw", Origin.TOOL).text == "ran with <SECRET_1>"   # the cached result follows
    g.store.add({"<PERSON_9>": "Bob"})                                               # under 4 chars: not searched
    assert g.scan("Bob was here", Origin.TOOL).text == "Bob was here"


def test_huge_text_is_scanned_in_chunks(tmp_path, analyzer):
    """C2(a): over spaCy's 1,000,000-character limit the scan still runs and finds every phone."""
    g = Gate(Config(data_dir=tmp_path), analyzer=analyzer, store=MappingStore(tmp_path / "m.json"))
    text = ("x" * 199 + "\n") * 999 + "call 555-010-0199\n"
    text = text * 6                                   # 1.2M characters
    r = g.scan(text, Origin.TOOL)
    assert len(text) > 1_000_000 and not r.reason.startswith("detector_error")
    assert "555-010-0199" not in r.text and r.text.count("<PHONE_NUMBER_1>") == 6


def test_chunk_boundary_keeps_a_private_key_block_whole(tmp_path, analyzer):
    from hermie.gate import gate as gate_mod
    g = Gate(Config(data_dir=tmp_path), analyzer=analyzer, store=MappingStore(tmp_path / "m.json"))
    key = "-----BEGIN RSA PRIVATE KEY-----\n" + "\n".join(["MIIEowIBAAKCAQEAexampleEXAMPLEexample0123"] * 40) \
          + "\n-----END RSA PRIVATE KEY-----\n"
    pad = gate_mod.CHUNK_CHARS - len(key) // 2
    text = ("y" * 99 + "\n") * (pad // 100) + key + ("z" * 99 + "\n") * 600
    r = g.scan(text, Origin.TOOL)
    assert "MIIEow" not in r.text and "END RSA PRIVATE KEY" not in r.text


def test_long_dense_text_scans_in_bounded_time(tmp_path, analyzer):
    """I2: 300 KB with about 3000 phone numbers."""
    import time
    g = Gate(Config(data_dir=tmp_path), analyzer=analyzer, store=MappingStore(tmp_path / "m.json"))
    lines, i = [], 0
    while sum(map(len, lines)) < 300_000:
        lines.append(f"2026-10-06 12:00:{i % 60:02d} INFO worker-{i % 7} order {i} customer 555-01{i // 1000 % 10}-{i % 10000:04d}, status ok, retries 0\n")
        i += 1
    t0 = time.monotonic()
    r = g.scan("".join(lines), Origin.TOOL)
    assert time.monotonic() - t0 < 20
    assert i >= 2900 and "555-01" not in r.text


def test_forget_never_reuses_a_number(tmp_path, analyzer):
    """C5: forget while serve runs: numbering continues, the cache is dropped, nothing restores to the wrong value."""
    path = tmp_path / "m.json"
    g = Gate(Config(data_dir=tmp_path), analyzer=analyzer, store=MappingStore(path))
    a_text = "call 555-010-0199"
    assert g.scan(a_text, Origin.TOOL).text == "call <PHONE_NUMBER_1>"
    MappingStore(path).clear()                                    # `hermie forget` from another process
    assert g.scan("call 555-010-0142", Origin.TOOL).text == "call <PHONE_NUMBER_2>"
    again = g.scan(a_text, Origin.TOOL)
    assert not again.cached and again.text == "call <PHONE_NUMBER_3>"
    assert g.restore(again.text)[0] == a_text
    assert g.restore("<PHONE_NUMBER_2>")[0] == "555-010-0142"
    data = json.loads(path.read_text())
    assert data["counters"] == {"PHONE_NUMBER": 3} and data["generation"] == 1


def test_store_reads_and_migrates_the_flat_format(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(json.dumps({"<PHONE_NUMBER_4>": "555-010-0199"}))
    s = MappingStore(path)
    assert s.mapping == {"<PHONE_NUMBER_4>": "555-010-0199"} and s.counters == {"PHONE_NUMBER": 4} and s.generation == 0
    s.add({"<EMAIL_ADDRESS_1>": "a@example.com"})
    data = json.loads(path.read_text())
    assert data["mapping"] == {"<PHONE_NUMBER_4>": "555-010-0199", "<EMAIL_ADDRESS_1>": "a@example.com"}
    assert data["counters"] == {"PHONE_NUMBER": 4, "EMAIL_ADDRESS": 1} and data["generation"] == 0
    s.clear()
    assert s.mapping == {} and s.counters == {"PHONE_NUMBER": 4, "EMAIL_ADDRESS": 1} and s.generation == 1


def test_smuggling_text_rule_on_leaves_url_rule_on_urls(tmp_path, analyzer):
    """C4: long prose, a git SHA and a build id are not smuggling; an encoded blob in text is; URLs keep their rules."""
    j = FakeJudge()
    g = Gate(Config(data_dir=tmp_path, judge="ollama:x"), analyzer=analyzer, judge=j, store=MappingStore(tmp_path / "m.json"))
    prose = ("The build system compiles every module in dependency order, then runs the unit tests and the "
             "integration tests against a local database. ") * 6
    for text in (prose, "commit 9fceb02d0ae598e95dc970b74767f19372d61af8 fixed it", "build id 20261006123456 passed"):
        assert len(prose) > 512
        r = g.scan(text, Origin.USER)
        assert not r.sensitive and r.reason == "clean", (text[:40], r.reason)
    blob = "UEsDBBQAAAAIAGx3R1kAAAAAAAAAAAAAAAAJAAAAZGF0YS5jc3ZLzs8tKEotLs5MT0ksSQUA"
    r = g.scan(f"please keep this for later: {blob}", Origin.USER)
    assert r.sensitive and r.reason.startswith("smuggling: possible base64")
    url = "https://x.test/?q=" + "a" * 600
    assert g.scan(url, Origin.TOOL).reason.startswith("smuggling: query string too long")
