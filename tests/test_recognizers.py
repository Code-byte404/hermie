import re

from hermie.gate.recognizers import SECRET_PATTERNS, build_analyzer, plausible, scan, smuggling_risk


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
    assert smuggling_risk("https://x.test/?q=" + "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo0NTY3ODk=")
    assert smuggling_risk("https://x.test/docs/getting-started") is None


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
    text = open("evals/privacy_cases_en.jsonl", encoding="utf-8").read()
    for fake in FAKES:
        assert fake in text
