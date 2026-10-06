import json
import re
from pathlib import Path

from hermie.gate.recognizers import (SECRET_PATTERNS, build_analyzer, plausible, scan, smuggling_risk_text,
                                     smuggling_risk_url)

EVALS = Path(__file__).resolve().parent.parent / "evals"


def ents(analyzer, text, langs=("en",)):
    return {f.entity: text[f.start:f.end] for f in scan(analyzer, text, langs, 0.5)}


def test_private_key_block_is_one_secret(analyzer):
    text = "key:\n-----BEGIN RSA PRIVATE KEY-----\nMIIEow==\nAB==\n-----END RSA PRIVATE KEY-----\nrest"
    f = [x for x in scan(analyzer, text, ("en",), 0.5) if x.entity == "SECRET"]
    assert len(f) == 1 and text[f[0].start:f[0].end].endswith("-----END RSA PRIVATE KEY-----")


def test_env_line_keeps_key_name(analyzer):
    text = "DATABASE_PASSWORD=Tr0ub4dor&3xyz\nDEBUG=true"
    f = ents(analyzer, text)
    assert f["SECRET"] == "Tr0ub4dor&3xyz" and "DATABASE_PASSWORD" in text


def test_db_url_credentials(analyzer):
    text = "DATABASE_URL=postgres://app:hunter2secret@db.internal:5432/prod"
    assert ents(analyzer, text)["SECRET"] == "hunter2secret"


def test_person_needs_two_capitalized_words():
    assert plausible("PERSON", "Maria Gonzalez", "en")
    assert not plausible("PERSON", "RequestHandler", "en")
    assert not plausible("PERSON", "Smith", "en")
    assert not plausible("PERSON", "user_name Foo", "en")


def test_org_and_location_are_off_by_default(analyzer):
    found = ents(analyzer, "We met Microsoft in Seattle last week")
    assert "ORGANIZATION" not in found and "LOCATION" not in found


def test_zh_engine_optional(analyzer_zh):
    assert "CN_MOBILE" in ents(analyzer_zh, "Customer phone 13812345678", ("en", "zh"))


def test_deny_words_become_custom_keyword():
    a = build_analyzer(("en",), deny_words=("ProjectCodenameA",))
    assert ents(a, "status of ProjectCodenameA")["CUSTOM_KEYWORD"] == "ProjectCodenameA"


def test_smuggling_risk_moved_intact():
    assert smuggling_risk_url("https://x.test/?q=" + "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo0NTY3ODk=")
    assert smuggling_risk_url("https://x.test/docs/getting-started") is None


def test_smuggling_text_variant_flags_only_encoded_tokens():
    """C4: no length rule and no digit-run rule for text; the eval negatives stay clean."""
    negatives = [c["text"] for c in map(json.loads, (EVALS / "privacy_cases_en.jsonl").read_text().splitlines())
                 if c.get("smuggling_negative")]
    assert len(negatives) >= 3 and any(len(t) >= 700 for t in negatives)
    for text in negatives:
        assert smuggling_risk_text(text) is None, text[:60]
    assert smuggling_risk_text("x" * 5000 + " 12345678901234567890") is None
    assert smuggling_risk_text("note: QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo0NTY3ODk=").startswith("possible base64")
    assert smuggling_risk_text("dump 3f9a1c0be7d24865af0c9e1b7d3a5f2c8e6b4d0a9f1e3c5b7d9f0a2c4e6b8d0f") is None
    # the URL rules are unchanged: a long query is still a risk there
    assert smuggling_risk_url("https://x.test/?q=" + "a" * 600).startswith("query string too long")


# Fake values used in the eval files: each must match exactly the named pattern and no other, so a fixture
# never doubles as a live-looking key of another provider.
FAKES = {
    "AKIAIOSFODNN7EXAMPLE": {"aws_access_key"},
    "sk-ant-api03-EXAMPLEexampleEXAMPLEexample0123456789AB": {"anthropic_key", "openai_style_key"},
    "ghp_EXAMPLEexampleEXAMPLEexample012345": {"github_token"},
    "sk_live_EXAMPLEexample01234567": {"stripe_key"},
    "hf_EXAMPLEexampleEXAMPLEexample012345": {"hf_token"},
    "npm_EXAMPLEexampleEXAMPLEexample0123456": {"npm_token"},
    "sk-test-not-a-real-key": {"openai_style_key"},
}


def test_fake_secret_fixtures_never_match_live_formats():
    for fake, expected in FAKES.items():
        matched = {name for name, rx, _ in SECRET_PATTERNS if re.search(rx, fake)}
        assert matched == expected, (fake, matched)
    # the fakes really are the ones the eval file uses
    text = (EVALS / "privacy_cases_en.jsonl").read_text(encoding="utf-8")
    for fake in FAKES:
        assert fake in text
