"""Wording: what people read about Hermie's decisions. Real values appear only where a person reads them locally."""
from types import SimpleNamespace

from hermie import explain

M = {"<PERSON_1>": "Alice Chen", "<PHONE_NUMBER_1>": "555-010-0199", "<SECRET_1>": "sk-test-abcdef123456f3a",
     "<EMAIL_ADDRESS_1>": "alice@example.com"}


def _line(**kw):
    base = dict(placeholders=[], withheld_items=[], withheld=[], held=None, approved_by=None, mode="enforce",
                replaced={}, restored=0)
    return SimpleNamespace(**(base | kw))


def test_reason_text_and_short_reason_never_echo_content():
    for reason in ("judge", "smuggling: possible base64-encoded data (44 chars)", "image", "detector_error: Boom",
                   "rejected earlier", "smuggling: possible hex data: 3f9a1c0be7d2..."):
        assert "3f9a" not in explain.reason_text(reason) and "Boom" not in explain.reason_text(reason)
        assert explain.short_reason(reason) in explain.SHORT_REASONS
    assert explain.reason_text("judge").startswith("a local model judged it private")
    assert explain.short_reason("smuggling: x") == "encoded data"


def test_display_value_shortens_secrets_and_long_tokens():
    assert explain.display_value("<SECRET_1>", "sk-test-abcdef123456f3a") == "sk-tes…f3a"
    assert explain.display_value("<TOKEN_1>", "x" * 19 + "yz") == "xxxxxx…xyz"
    assert explain.display_value("<PERSON_1>", "Alice Chen") == "Alice Chen"
    assert explain.display_value("<PHONE_NUMBER_1>", "555-010-0199") == "555-010-0199"


def test_display_value_collapses_and_caps():
    v = explain.display_value("<ADDRESS_1>", "1 Main St\n\nSpringfield " + "a " * 200)
    assert "\n" not in v and len(v) <= 81 and v.endswith("…") and "1 Main St Springfield" in v


def test_value_pairs_limit_and_more():
    phs = ["<PERSON_1>", "<PHONE_NUMBER_1>", "<SECRET_1>", "<EMAIL_ADDRESS_1>", "<PERSON_2>", "<PERSON_3>", "<PERSON_4>"]
    pairs = explain.value_pairs(phs, M, limit=5)
    assert pairs[0] == ("Alice Chen", "<PERSON_1>") and pairs[2] == ("sk-tes…f3a", "<SECRET_1>")
    assert pairs[-1] == ("+2 more", "") and len(pairs) == 6
    assert explain.pairs_text(pairs).startswith("Alice Chen -> <PERSON_1>, 555-010-0199 -> <PHONE_NUMBER_1>")
    assert explain.names_text(pairs[:2]) == "Alice Chen, 555-010-0199"


def test_value_pairs_without_mapping_value_shows_name():
    pairs = explain.value_pairs(["<PERSON_9>"], {})
    assert pairs == [("", "<PERSON_9>")]
    assert explain.pairs_text(pairs) == "<PERSON_9>" and explain.names_text(pairs) == "<PERSON_9>"


def test_value_pairs_dedupes_in_order():
    assert [p for _, p in explain.value_pairs(["<PERSON_1>", "<PERSON_1>", "<SECRET_1>"], M)] == ["<PERSON_1>", "<SECRET_1>"]


def test_notes_for_the_model_carry_no_values_and_ask_to_tell_the_user():
    n = explain.note_text("tool", "9c21", size="1.2 KB", reason="smuggling: possible hex data: 3f9a1c...", tool="Read")
    assert n.startswith("[Hermie withheld this tool result (Read, 1.2 KB)") and "3f9a" not in n
    assert "Tell the user" in n and "hermie allow 9c21" in n
    assert "cannot be released" in explain.note_text("unscanned", "9c21", size="3 B", reason="detector_error: X")
    assert "hermie allow 9c21" in explain.note_text("image", "9c21")
    d = explain.note_text("dropped", "9c21")
    assert "chose not to send" in d and "rejected" not in d.lower()


def test_error_texts_name_the_cause_and_the_way_out():
    assert "you chose Don't send" in explain.error_text("reject", "ab12")
    assert "no answer within 60 seconds" in explain.error_text("timeout", "ab12", timeout_s=60)
    assert "no one was asked" in explain.error_text("unavailable", "ab12")
    for cause in ("reject", "timeout", "unavailable", "local_discarded", "local_failed"):
        t = explain.error_text(cause, "ab12", timeout_s=60)
        assert t.startswith("Hermie did not send this message: ") and "hermie allow ab12" in t
        assert "Nothing left this machine." in t and "blocked" not in t
    assert "api.anthropic.com" in explain.unreachable_text("api.anthropic.com", "ConnectError")
    assert "/tmp/x/mapping.json" in explain.mapping_error_text("/tmp/x/mapping.json", "PermissionError")


def test_held_view_explains_each_choice_and_shows_values():
    row = {"id": "ab12", "kind": "message", "reason": "judge", "size": 80, "local": True,
           "excerpt": "lay off <PERSON_1>'s team", "placeholders": ["<PERSON_1>"], "deadline": 1000.0}
    v = explain.held_view(row, M)
    assert v["title"] == "Hermie stopped your message before it left this machine"
    assert v["why"].startswith("a local model judged it private") and v["pairs"] == [["Alice Chen", "<PERSON_1>"]]
    assert [o["value"] for o in v["options"]] == ["reject", "send", "session", "local"]
    assert v["options"][0]["label"] == "Don't send" and "error" in v["options"][0]["note"]
    assert "not asked about again" in v["options"][1]["note"] and "restarts" in v["options"][2]["note"]
    assert v["deadline"] == 1000.0
    assert [o["value"] for o in explain.held_view(row | {"local": False}, M)["options"]] == ["reject", "send", "session"]


def test_held_view_for_a_rewrite_review():
    row = {"id": "ab12", "kind": "rewrite", "reason": "judge", "size": 20, "preview": "lay off <PERSON_1>",
           "deadline": 5.0}
    v = explain.held_view(row, M)
    assert v["title"] == "Your message with the specifics hidden: this is what would be sent"
    assert v["excerpt"] == "lay off <PERSON_1>" and v["pairs"] == [["Alice Chen", "<PERSON_1>"]]
    assert [o["label"] for o in v["options"]] == ["Don't send", "Redo", "Send this"]


def test_held_lines_for_terminal():
    row = {"id": "ab12", "kind": "message", "reason": "judge", "size": 80, "excerpt": "hi <PERSON_1>",
           "placeholders": ["<PERSON_1>"], "deadline": 0}
    lines = explain.held_lines(explain.held_view(row, M), {"reject": "n", "send": "s", "session": "a"})
    text = "\n".join(lines)
    assert "Why: a local model judged it private" in text and '"hi <PERSON_1>"' in text
    assert "Already replaced: Alice Chen -> <PERSON_1>" in text and "[n] Don't send" in text
    assert "hermie allow ab12" in text


def test_turn_summary_lists_values_withheld_items_and_true_closing_line():
    lines = [_line(placeholders=["<PERSON_1>", "<SECRET_1>"], replaced={"PERSON": 1, "SECRET": 1}),
             _line(placeholders=["<PERSON_1>"], withheld=["9c21"],
                   withheld_items=[{"id": "9c21", "tool": "Read", "file": "customers.csv", "why": "judged private"}])]
    s = explain.turn_summary(lines, M)
    assert s.startswith("Hermie · this turn (2 requests)")
    assert "Alice Chen -> <PERSON_1>" in s and "sk-tes…f3a -> <SECRET_1>" in s and s.count("Alice Chen") == 1
    assert "Read customers.csv (judged private)" in s and "hermie allow 9c21" in s
    assert s.endswith("Only placeholders left this machine.")


def test_turn_summary_closing_line_only_when_true():
    base = dict(placeholders=["<PERSON_1>"], replaced={"PERSON": 1})
    for kw in ({"approved_by": "user"}, {"approved_by": "session"}, {"approved_by": "allowed"}):
        s = explain.turn_summary([_line(**base, **kw)], M)
        assert "Only placeholders" not in s and "you approved" in s
    assert "Only placeholders" in explain.turn_summary([_line(**base, approved_by="local")], M)
    obs = explain.turn_summary([_line(**base, mode="observe")], M)
    assert "Observe mode" in obs and "Only placeholders" not in obs


def test_turn_summary_held_not_sent_and_quiet_turns():
    s = explain.turn_summary([_line(held="ab12")], M)
    assert "held" in s and "not sent" in s and "hermie allow ab12" in s
    assert explain.turn_summary([_line()], M) is None


def test_turn_summary_reads_old_lines_without_new_fields():
    old = SimpleNamespace(withheld=["9c21"], held=None, approved_by=None, mode="enforce", replaced={"PERSON": 2},
                          restored=1)
    s = explain.turn_summary([old], M)
    assert "2 PERSON" in s and "hermie allow 9c21" in s


def test_last_text_separates_held_and_withheld():
    t = explain.last_text(_line(placeholders=["<PERSON_1>", "<PHONE_NUMBER_1>"], withheld=["9c21"], held="ab12"), M)
    assert t == ("2 replaced (Alice Chen, 555-010-0199) · 1 withheld -> hermie allow 9c21 · "
                 "1 held, not sent -> hermie allow ab12")
    assert explain.last_text(_line(), M) == "nothing replaced"


def test_last_text_says_how_many_values_were_put_back():
    assert explain.last_text(_line(placeholders=["<PERSON_1>"], restored=2), M) == "1 replaced (Alice Chen) · 2 put back"
