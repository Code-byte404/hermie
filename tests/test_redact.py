import os, re
from pathlib import Path
import pytest
from hypothesis import given, strategies as st
from hermie.gate.types import Finding, CleanBody
from hermie.gate.redact import redact, restore, MappingStore, MappingStoreError, PLACEHOLDER

def _findings(text, *spans):
    return [Finding(e, text.index(s), text.index(s) + len(s), 0.9) for e, s in spans]

def test_redact_then_restore_roundtrip():
    text = "call 555-010-0199 or mail a@example.com, again 555-010-0199"
    red, mapping = redact(text, _findings(text, ("PHONE_NUMBER", "555-010-0199"), ("EMAIL_ADDRESS", "a@example.com")))
    assert red == "call <PHONE_NUMBER_1> or mail <EMAIL_ADDRESS_1>, again 555-010-0199"  # only the first span given
    assert restore(red, mapping) == ("call 555-010-0199 or mail a@example.com, again 555-010-0199", 0)

def test_existing_mapping_keeps_numbering():
    red, m = redact("x 555-010-0199", [Finding("PHONE_NUMBER", 2, 14, 0.9)], existing={"<PHONE_NUMBER_7>": "555-010-0188"})
    assert red == "x <PHONE_NUMBER_8>" and m == {"<PHONE_NUMBER_8>": "555-010-0199"}
    red2, m2 = redact("x 555-010-0188", [Finding("PHONE_NUMBER", 2, 14, 0.9)], existing={"<PHONE_NUMBER_7>": "555-010-0188"})
    assert red2 == "x <PHONE_NUMBER_7>" and m2 == {}

def test_restore_counts_unknown_placeholders_and_leaves_them():
    assert restore("a <PHONE_NUMBER_3> b <FOO_1>", {"<PHONE_NUMBER_3>": "1"}) == ("a 1 b <FOO_1>", 1)

def test_hand_typed_placeholder_in_input_passes_through():
    # Review Focus 3: a placeholder pasted by the user is not a finding and is not re-minted
    red, m = redact("please fix <PHONE_NUMBER_3>", [])
    assert red == "please fix <PHONE_NUMBER_3>" and m == {}

@given(st.lists(st.sampled_from(["555-010-0199", "+1 555 010 0199", "a@example.com", "4242424242424242"]), min_size=1, max_size=6),
       st.lists(st.text(alphabet="abc ,.", min_size=0, max_size=5), min_size=7, max_size=7))
def test_property_roundtrip_with_overlaps(values, fillers):
    text = "".join(f + v for f, v in zip(fillers, values + [""] * 7))
    findings = []
    for v in set(values):
        for mt in re.finditer(re.escape(v), text):
            findings.append(Finding("X", mt.start(), mt.end(), 0.9))
        core = v.replace("+1 ", "")   # Review Focus 4: substring findings overlap
        for mt in re.finditer(re.escape(core), text):
            findings.append(Finding("Y", mt.start(), mt.end(), 0.9))
    red, mapping = redact(text, findings)
    assert restore(red, mapping)[0] == text
    assert not any(v in red for v in values)

def test_mapping_store_persists_merges_and_is_private(tmp_path):
    p = tmp_path / "mapping.json"
    a, b = MappingStore(p), MappingStore(p)
    a.add({"<PHONE_NUMBER_1>": "555-010-0199"})
    b.add({"<EMAIL_ADDRESS_1>": "a@example.com"})
    assert MappingStore(p).mapping == {"<PHONE_NUMBER_1>": "555-010-0199", "<EMAIL_ADDRESS_1>": "a@example.com"}
    assert oct(p.stat().st_mode & 0o777) == "0o600"
    a.clear(); assert MappingStore(p).mapping == {}

def test_mapping_store_unwritable_raises(tmp_path):
    d = tmp_path / "ro"; d.mkdir(); os.chmod(d, 0o500)
    try:
        with pytest.raises(MappingStoreError):
            MappingStore(d / "mapping.json").add({"<X_1>": "y"})
    finally:
        os.chmod(d, 0o700)

def test_cleanbody_cannot_be_built_outside_the_gate():
    with pytest.raises(PermissionError):
        CleanBody(b"{}")
