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


class SeeingJudge(FakeJudge):
    def __init__(self, **kw): super().__init__(**kw); self.seen = []
    def is_sensitive(self, text): self.seen.append(text); return super().is_sensitive(text)

def _judged(tmp_path, analyzer, j):
    return Gate(Config(data_dir=tmp_path, judge="ollama:x"), analyzer=analyzer, judge=j, store=MappingStore(tmp_path / "m.json"))

def test_judge_skips_injected_system_reminder_blocks(tmp_path, analyzer):
    j = SeeingJudge(sensitive=True)
    g = _judged(tmp_path, analyzer, j)
    only = "<system-reminder>\nUser instructions: the patient diagnosis notes live in docs/\n</system-reminder>"
    r = g.scan(only, Origin.USER)
    assert not r.sensitive and j.calls == 0          # nothing left for the judge to look at
    mixed = "<system-reminder>project notes</system-reminder>\nthe layoff list is final"
    r = g.scan(mixed, Origin.USER)
    assert r.sensitive and j.seen == ["\nthe layoff list is final"]   # the user's own text is still judged

def test_system_reminder_blocks_still_get_pattern_redaction(tmp_path, analyzer):
    g = _judged(tmp_path, analyzer, SeeingJudge())
    r = g.scan("<system-reminder>call 555-010-0199</system-reminder>", Origin.USER)
    assert "555-010-0199" not in r.text and r.findings


def test_strip_injected_removes_blocks_and_keeps_the_rest():
    from hermie.gate.gate import strip_injected
    assert strip_injected("<system-reminder>a\nb</system-reminder>\nhello") == "\nhello"
    assert strip_injected("no blocks") == "no blocks"

_SCRATCH = "/private/tmp/claude-501/-Users-robinz-Documents-HybridAgent/469517cf-378a-42a8-86c6-d8dae55a83d4/scratchpad"

def test_injected_blocks_and_plain_paths_are_not_smuggling(tmp_path, analyzer):
    g = _judged(tmp_path, analyzer, SeeingJudge())
    for text in (f"<system-reminder>Scratchpad directory: {_SCRATCH}</system-reminder>\nwrite api key",
                 f"keep temp files in {_SCRATCH} please"):
        r = g.scan(text, Origin.USER)
        assert not r.sensitive, r.reason

def test_a_key_in_a_message_is_replaced_not_held(tmp_path, analyzer):
    g = _judged(tmp_path, analyzer, SeeingJudge())
    r = g.scan(f"<system-reminder>dir {_SCRATCH}</system-reminder>\nwrite api key to file : sk-aavvvvbbbbbdfsfdsadf", Origin.USER)
    assert not r.sensitive and "sk-aavvvvbbbbbdfsfdsadf" not in r.text and r.findings


def test_judge_verdicts_survive_a_restart_and_hold_no_text(tmp_path, analyzer):
    text = "the layoff list is final and confidential"
    j1 = FakeJudge(sensitive=True)
    g1 = Gate(Config(data_dir=tmp_path, judge="ollama:x"), analyzer=analyzer, judge=j1, store=MappingStore(tmp_path / "m.json"))
    assert g1.scan(text, Origin.USER).sensitive and j1.calls == 1
    j2 = FakeJudge(sensitive=False)   # would answer differently: the stored verdict must win
    g2 = Gate(Config(data_dir=tmp_path, judge="ollama:x"), analyzer=analyzer, judge=j2, store=MappingStore(tmp_path / "m.json"))
    assert g2.scan(text, Origin.USER).sensitive and j2.calls == 0
    assert "layoff" not in (tmp_path / "judge_cache.jsonl").read_text()
    j3 = FakeJudge(sensitive=False)   # another judge model: verdicts do not carry over
    g3 = Gate(Config(data_dir=tmp_path, judge="laya:/m/v3"), analyzer=analyzer, judge=j3, store=MappingStore(tmp_path / "m.json"))
    assert not g3.scan(text, Origin.USER).sensitive and j3.calls == 1

def test_a_judge_error_is_never_stored(tmp_path, analyzer):
    g = Gate(Config(data_dir=tmp_path, judge="ollama:x"), analyzer=analyzer, judge=FakeJudge(boom=True), store=MappingStore(tmp_path / "m.json"))
    assert g.scan("anything", Origin.USER).reason.startswith("detector_error")
    p = tmp_path / "judge_cache.jsonl"
    assert not p.exists() or p.read_text() == ""


def test_system_reminder_blocks_are_skipped_only_in_the_users_own_turn(tmp_path, analyzer):
    # a file or a tool can wrap anything in these tags; only the client puts them into a user message
    j = SeeingJudge(sensitive=True)
    g = _judged(tmp_path, analyzer, j)
    wrapped = "<system-reminder>the patient was diagnosed last week</system-reminder>"
    r = g.scan(wrapped, Origin.TOOL)
    assert r.sensitive and r.reason == "judge" and j.seen == [wrapped]


def test_local_spans_match_whole_words_and_never_inside_a_placeholder(gate):
    text = "Lin and Linda met; <PERSON_7> left. ID card of Lin."
    red = gate.redact_spans(text, [("Lin", "PERSON"), ("SON_7", "ID"), ("ID", "ID")])
    assert red.count("<PERSON_") == 3 and "Linda" in red and "<PERSON_7>" in red
    assert "ID card" in red          # under 3 characters: dropped
    assert "SON_7" not in gate.store.mapping.values()


def test_local_spans_in_chinese_text_need_no_word_boundary(gate):
    red = gate.redact_spans("同事王芳明天请假", [("王芳", "PERSON")])
    assert "王芳" not in red and "<PERSON_" in red


def test_generic_word_span_restores_but_is_not_searched_later(gate, tmp_path):
    red = gate.redact_spans("the layoff hits Alice Chen", [("layoff", "TOPIC"), ("Alice Chen", "PERSON")])
    assert "layoff" not in red and "Alice Chen" not in red
    ph = next(k for k, v in gate.store.mapping.items() if v == "layoff")
    assert gate.restore(f"about {ph}")[0] == "about layoff"
    for g in (gate, Gate(gate.c, analyzer=gate._analyzer, store=MappingStore(gate.c.mapping_path))):   # and after a restart
        r = g.scan("no layoff talk; ask Alice Chen", Origin.TOOL)
        assert "layoff" in r.text and "Alice Chen" not in r.text


def test_hide_makes_a_quiet_value_searched_again(gate, tmp_path):
    from hermie import cli
    gate.redact_spans("the layoff", [("layoff", "TOPIC")])
    assert cli.main(["hide", "layoff", "--data-dir", str(tmp_path)]) == 0
    assert "layoff" not in gate.scan("a layoff", Origin.TOOL).text


def _key_lines(seed):
    import base64
    import random
    body = base64.b64encode(random.Random(seed).randbytes(1600)).decode()
    return ["MIIEowIBAAKCAQEA" + body[16:64]] + [body[i:i + 64] for i in range(64, len(body), 64)]


def test_chunk_boundary_inside_a_concatenated_or_escaped_key_hides_every_line(tmp_path, analyzer):
    from hermie.gate import gate as gate_mod
    g = Gate(Config(data_dir=tmp_path), analyzer=analyzer, store=MappingStore(tmp_path / "m.json"))
    lines = _key_lines(3)
    js = '"-----BEGIN RSA PRIVATE KEY-----\\n" +\n' + "".join('"' + x + '\\n" +\n' for x in lines) \
         + '"-----END RSA PRIVATE KEY-----\\n";\n'
    esc = "-----BEGIN RSA PRIVATE KEY-----\\n" + "\\n".join(lines) + "\\n-----END RSA PRIVATE KEY-----\\n"
    for text in [("word " * 15 + "\n") * ((gate_mod.CHUNK_CHARS - 720) // 76) + js,
                 '{"a": "' + "x y " * ((gate_mod.CHUNK_CHARS - 700) // 4) + esc + '"}']:
        fs = [f for f in g._pattern_findings(text) if f.entity == "SECRET"]
        missed = [x for x in lines if not any(f.start <= text.find(x) and text.find(x) + len(x) <= f.end for f in fs)]
        assert missed == [], len(missed)


def test_names_sharing_a_column_with_a_known_person_are_replaced(tmp_path, analyzer):
    store = MappingStore(tmp_path / "mapping.json")
    store.add({"<PERSON_1>": "Olivia Gallagher"})
    g = Gate(Config(data_dir=tmp_path), analyzer=analyzer, store=store)
    zh = "卢静怡"
    text = f"Olivia Gallagher | Operations | 181200\n{zh} | Finance | 168400\nDaniel Fairbanks | Legal | 161200\n"
    r = g.scan(text, Origin.TOOL)
    assert "Olivia Gallagher" not in r.text and zh not in r.text and "Daniel Fairbanks" not in r.text
    assert "Operations" in r.text and "Finance" in r.text
