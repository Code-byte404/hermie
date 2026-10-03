"""Pure-function policy + privacy gate + type constraints."""
import pytest

from hermie.agents import require_clean
from hermie.capabilities import rule_risk
from hermie.config import Settings
from hermie.judge import ChoiceAnswer, ScoreAnswer
from hermie.policy import Force, Route, Signals, decide
from hermie.privacy import CleanText, PrivacyGate

from .conftest import FakeJudge

S = Settings(routellm_enabled=False)


def sig(task="simple", conf=0.9, cx=0, cx_conf=0.9, sensitive=False, win=None, ws=True):
    return Signals(sensitive, ChoiceAnswer(task, {}, conf), ScoreAnswer(cx, [], cx_conf), win, ws)


@pytest.mark.parametrize("s,expected", [
    (sig(task="repetitive", cx=2), Route.LOCAL),                       # repetitive work is always local, regardless of difficulty
    (sig(task="planning"), Route.PLAN),                                # planning + needs local operations
    (sig(task="planning", ws=False), Route.CLOUD),                     # planning + no local operations: cloud direct
    (sig(task="planning", sensitive=True, ws=False), Route.PLAN),      # private data: plan mode is the only option
    (sig(task="simple", sensitive=True), Route.LOCAL),                 # private data and no planning needed: local only
    (sig(task="complex", conf=0.4, cx=1, cx_conf=0.4), Route.LOCAL_VERIFY),  # weak signals
    (sig(task="complex", cx=1, win=0.8, ws=False), Route.CLOUD),       # complex + high RouteLLM win rate
    (sig(task="simple", cx=1, win=0.1), Route.LOCAL),                  # simple + RouteLLM says local is enough
    (sig(task="complex", cx=2, cx_conf=0.9), Route.PLAN),              # hard
])
def test_decide(s, expected):
    assert decide(s, S).route is expected


def test_force():
    assert decide(sig(task="planning"), S, Force.LOCAL).route is Route.LOCAL
    assert decide(sig(task="repetitive", ws=False), S, Force.CLOUD).route is Route.CLOUD
    assert decide(sig(task="repetitive", sensitive=True), S, Force.CLOUD).route is Route.PLAN  # forced cloud still passes the gate


def test_cleantext_cannot_be_forged():
    with pytest.raises(PermissionError):
        CleanText("secret")
    with pytest.raises(TypeError):
        require_clean("raw text")


@pytest.fixture
def gate(analyzer):
    return PrivacyGate(Settings(), judge=FakeJudge(secrets=("layoffs",)), analyzer=analyzer)


@pytest.mark.parametrize("text,entity", [
    ("customer phone 13812345678", "CN_MOBILE"),
    ("ID card 11010519491231002X", "CN_ID_CARD"),
    ("email zhang@example.com", "EMAIL_ADDRESS"),
    ("bank card 6222021234567890128", "BANK_CARD"),   # valid Luhn
    ("server 10.0.0.12", "IP_ADDRESS"),
])
def test_gate_detects_entities(gate, text, entity):
    v = gate.check(text)
    assert v.sensitive and entity in {f.entity for f in v.findings}


def test_gate_detects_custom_keyword(gate, analyzer):
    # The keyword comes from the session analyzer fixture (conftest), whatever it is configured with
    kw = next(r.deny_list[0] for r in analyzer.registry.recognizers if "CUSTOM_KEYWORD" in r.supported_entities)
    v = gate.check(f"progress on {kw} so far")
    assert v.sensitive and "CUSTOM_KEYWORD" in {f.entity for f in v.findings}


def test_gate_rejects_invalid_checksums(gate):
    assert not gate.check("reference 110105194912310021 and 6222021234567890123").sensitive  # bad check digits


def test_gate_contextual_and_fail_closed(gate, analyzer):
    assert gate.check("we plan layoffs of 30% next quarter").contextual
    assert not gate.check("explain quicksort").sensitive
    broken = PrivacyGate(Settings(), judge=FakeJudge(fail_privacy=True), analyzer=analyzer)
    assert broken.check("explain quicksort").sensitive  # a judge failure is treated as private
    with pytest.raises(PermissionError):
        broken.certify("explain quicksort")


def test_redact_and_restore(gate):
    t = "call 13812345678, then call 13812345678 again; ID card 11010519491231002X"
    v = gate.check(t)
    red, mapping = gate.redact(t, v.findings)
    assert "13812345678" not in red and red.count("<CN_MOBILE_1>") == 2
    assert gate.restore(red, mapping) == t
    gate.certify(red)  # the redacted result may go out


@pytest.mark.parametrize("cmd,risk", [
    ("ls -la", "low"), ("cat a.txt | wc -l", "low"), ("git status", "low"),
    ("rm -rf build", "high"), ("pip install requests", "high"), ("curl https://x", "high"),
    ("git push origin main", "high"), ("echo hi > a.txt", None), ("./run.sh", None),
])
def test_rule_risk(cmd, risk):
    assert rule_risk(cmd) == risk


def test_ner_false_positives_filtered(gate):
    assert not gate.check("translate data/titles.txt and write it to out/titles_en.txt", use_judge=False).sensitive
    assert not gate.check("How to Build a Local-First AI Agent", use_judge=False).sensitive
    assert not gate.check('{"issues": []}', use_judge=False).sensitive


@pytest.mark.parametrize("text", [
    "DEEPSEEK_API_KEY=sk-EXAMPLEKEYnotreal00000000EXAMPLE",
    "aws uses AKIAIOSFODNN7EXAMPLE for this",
    "token ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEow...",
    "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    "database password: Xk9#mP2vQ8zL",
    "in the config api_key = \"abcd1234efgh5678\"",
    # Chinese "secret key" keyword + full-width colon (the credential_assignment regex also covers them);
    # written as \u escapes so the source file stays ASCII
    "\u5bc6\u94a5\uff1aAbCdEf123456",
])
def test_gate_detects_secrets(gate, text):
    v = gate.check(text, use_judge=False)
    assert v.sensitive and "SECRET" in {f.entity for f in v.findings}, text


@pytest.mark.parametrize("text", [
    "MAX_TOKENS=4096 SESSION_TIMEOUT=30",
    "count the token usage",
    "this code validates using the password field",
    "skip-permissions mode",
])
def test_secret_recognizer_no_false_positive(gate, text):
    assert not gate.check(text, use_judge=False).sensitive, text


@pytest.mark.parametrize("cmd", [
    'python -c "import shutil; shutil.rmtree(\'.\')"', "python3 -c 'print(1)'", "node -e 'process.exit()'",
    "bash -c 'rm -rf x'", "python -m pytest -c foo",
])
def test_inline_code_goes_to_judge(cmd):
    assert rule_risk(cmd) is None


@pytest.mark.parametrize("cmd", ["python script.py", "python3 -m pytest -q"])
def test_plain_scripts_stay_low(cmd):
    assert rule_risk(cmd) == "low"


def test_redact_continues_existing_numbering(analyzer):
    from hermie.privacy import PrivacyGate
    gate = PrivacyGate(Settings(), judge=None, analyzer=analyzer)
    first = gate.check("call 13812345678", use_judge=False)
    t1, m1 = PrivacyGate.redact("call 13812345678", first.findings)
    assert m1 == {"<CN_MOBILE_1>": "13812345678"}
    text = "use 13987654321 or 13812345678"
    v = gate.check(text, use_judge=False)
    t2, m2 = PrivacyGate.redact(text, v.findings, existing=m1)
    assert t2 == "use <CN_MOBILE_2> or <CN_MOBILE_1>"
    assert m2 == {"<CN_MOBILE_2>": "13987654321"}      # only the new placeholder


def _business_sig(**kw):
    base = dict(sensitive=False, task=ChoiceAnswer("planning", {"planning": 0.9}, 0.9),
                complexity=ScoreAnswer(2, [0, 0, 1.0], 0.9), win_rate=0.9, needs_workspace=False, business=True)
    base.update(kw)
    return Signals(**base)


def test_business_is_local_under_every_force(settings):
    for force in Force:
        d = decide(_business_sig(), settings, force)
        assert d.route is Route.LOCAL and d.reasons[0] == "business data: local only"
    d = decide(_business_sig(), settings, Force.CLOUD)
    assert "never leaves this machine" in d.reasons[1]


def test_business_beats_sensitive_plan(settings):
    assert decide(_business_sig(sensitive=True, needs_workspace=True), settings).route is Route.LOCAL
