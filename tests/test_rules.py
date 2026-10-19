"""The pure-regex detectors (hermie/gate/rules.py): credentials of many providers in many shapes, contract-style personal
data in English and Chinese, and a corpus of ordinary text that must stay untouched.

Every credential is assembled at run time from a prefix and random characters, so no real-looking key is committed."""
import base64
import random
import string

import pytest

from hermie.gate import rules

rnd = random.Random(7)
ALNUM = string.ascii_letters + string.digits
HEX = "0123456789abcdef"
B64 = ALNUM + "+/"
B64URL = ALNUM + "-_"
UP = string.ascii_uppercase + string.digits


def r(n, alpha=ALNUM):
    return "".join(rnd.choice(alpha) for _ in range(n))


# name -> (variable name or None, value maker, the part that must be hidden)
KEYS = {
    "aws_secret": ("AWS_SECRET_ACCESS_KEY", lambda: r(40, B64), None),
    "github_fine": ("GH_TOKEN", lambda: "github_pat_" + r(22) + "_" + r(59), None),
    "gitlab": ("GITLAB_TOKEN", lambda: "glpat-" + r(20, B64URL), None),
    "slack_webhook": ("SLACK_WEBHOOK_URL", lambda: "https://hooks.slack.com/services/T" + r(8, UP) + "/B" + r(10, UP) + "/" + r(24),
                      lambda v: v.split("/services/")[1]),
    "stripe_whsec": ("STRIPE_WEBHOOK_SECRET", lambda: "whsec_" + r(32), None),
    "gcp_key_id": ("GCP_PRIVATE_KEY_ID", lambda: r(40, HEX), None),
    "azure_storage": ("AZURE_STORAGE_KEY", lambda: base64.b64encode(rnd.randbytes(64)).decode(), None),
    "twilio_auth": ("TWILIO_AUTH_TOKEN", lambda: r(32, HEX), None),
    "sendgrid": ("SENDGRID_API_KEY", lambda: "SG." + r(22, B64URL) + "." + r(43, B64URL), None),
    "mailgun": ("MAILGUN_API_KEY", lambda: "key-" + r(32, HEX), None),
    "cloudflare": ("CLOUDFLARE_API_TOKEN", lambda: r(40, B64URL), None),
    "vercel": ("VERCEL_TOKEN", lambda: r(24), None),
    "mongodb_url": ("MONGODB_URI", lambda: "mongodb+srv://admin:" + r(16) + "@cluster0.ab12c.mongodb.net/prod",
                    lambda v: v.split(":")[2].split("@")[0]),
    "postgres_url": ("DATABASE_URL", lambda: "postgresql://app:" + r(18) + "@db.internal:5432/app",
                     lambda v: v.split(":")[2].split("@")[0]),
    "redis_url": ("REDIS_URL", lambda: "redis://:" + r(20) + "@cache.internal:6379/0", lambda v: v.split(":")[2].split("@")[0]),
    "jwt_secret": ("JWT_SECRET", lambda: r(64, HEX), None),
    "session_secret": ("SESSION_SECRET", lambda: r(43, B64URL), None),
    "password": ("DB_PASSWORD", lambda: r(14, ALNUM + "!@#$%"), None),
    "telegram": ("TELEGRAM_BOT_TOKEN", lambda: r(9, string.digits) + ":" + r(35, B64URL), None),
    "heroku": ("HEROKU_API_KEY", lambda: "-".join([r(8, HEX), r(4, HEX), r(4, HEX), r(4, HEX), r(12, HEX)]), None),
    "datadog": ("DD_API_KEY", lambda: r(32, HEX), None),
    "sentry_dsn": ("SENTRY_DSN", lambda: "https://" + r(32, HEX) + "@o123456.ingest.sentry.io/4501234",
                   lambda v: v.split("//")[1].split("@")[0]),
    "tencent": ("TENCENT_SECRET_KEY", lambda: r(32), None),
    "aliyun": ("ALIYUN_ACCESS_KEY_SECRET", lambda: r(30), None),
    "wechat": ("WECHAT_APP_SECRET", lambda: r(32, HEX), None),
    "openai": ("OPENAI_API_KEY", lambda: "sk-" + r(48), None),
    "anthropic": ("ANTHROPIC_API_KEY", lambda: "sk-ant-api03-" + r(90, B64URL) + "AA", None),
    "aws_access": ("AWS_ACCESS_KEY_ID", lambda: "AKIA" + r(16, UP), None),
    "stripe_live": ("STRIPE_SECRET_KEY", lambda: "sk_live_" + r(24), None),
    "ssh_private": ("DEPLOY_KEY", lambda: "-----BEGIN OPENSSH PRIVATE KEY-----\n" + "\n".join(
        base64.b64encode(rnd.randbytes(48)).decode() for _ in range(5)) + "\n-----END OPENSSH PRIVATE KEY-----", None),
}
BARE = {"bearer": lambda: r(40, B64), "aws_pair": lambda: r(40, B64)}   # no variable name: only the context gives it away


def _contexts(var, v):
    out = {}
    one = "\n" not in v
    if var:
        out["env"] = f"{var}={v}"
        out["env_quoted"] = f'{var}="{v}"'
        out["export"] = f"export {var}={v}"
        out["json"] = '{"' + var.lower() + '": "' + v.replace("\n", "\\n") + '"}'
        out["json_camel"] = '{"' + "".join(p.capitalize() if i else p.lower() for i, p in enumerate(var.split("_"))) + '": "' + v.replace("\n", "\\n") + '"}'
        out["yaml"] = f"{var.lower()}: {v}" if one else f"{var.lower()}: |\n  " + v.replace("\n", "\n  ")
        out["python"] = f'{var.lower()} = "{v}"'.replace("\n", "\\n")
    out["prose"] = f"here is the key {v} please set it up" if one else f"here is the key:\n{v}\nplease set it up"
    if one:
        out["curl"] = f'curl -H "Authorization: Bearer {v}" https://api.example.com/v1/x'
    return out


def _covered(findings, text, core):
    start = text.index(core.split("\n")[0][:20])
    end = start + len(core)
    cover = sum(max(0, min(f.end, end) - max(f.start, start)) for f in findings)
    return cover >= 0.9 * len(core)


# A value with no shape of its own is only recognizable from its name or from the words around it, so it appears only in
# the contexts that carry such words; a value of a known shape (or a long random token) is also found in plain prose.
SHAPED_IN_PROSE = {"github_fine", "gitlab", "slack_webhook", "stripe_whsec", "sendgrid", "mailgun", "mongodb_url", "postgres_url", "redis_url",
                   "telegram", "sentry_dsn", "azure_storage", "openai", "anthropic", "aws_access", "stripe_live", "ssh_private", "aws_secret",
                   "cloudflare", "session_secret"}
NO_BEARER = {"password", "ssh_private"}
CASES = []
for name, (var, make, core_of) in KEYS.items():
    v = make()
    core = core_of(v) if core_of else v
    for ctx, text in _contexts(var, v).items():
        if ctx == "prose" and name not in SHAPED_IN_PROSE:
            continue
        if ctx == "curl" and name in NO_BEARER:
            continue
        CASES.append(pytest.param(text, core, id=f"{name}-{ctx}"))


@pytest.mark.parametrize("text,core", CASES)
def test_credentials_are_found_in_every_shape(text, core):
    assert _covered(rules.find(text), text, core), text[:120]


def test_bare_tokens_after_authorization_words_and_phrases_are_found():
    for text in (f"Authorization: Bearer {r(40, B64)}", f"curl -u admin:{r(16)} https://x.example.com", f"x-api-key: {r(36, B64URL)}",
                 f"GET /v1/items?api_key={r(32, HEX)}&limit=5", f"the password is {r(10, ALNUM + '!@#')}7x for the staging db",
                 f"AWS keys: AKIA{r(16, UP)} and {r(40, B64)}", "\u5bc6\u7801\u662f" + r(12) + "4", "\u6570\u636e\u5e93\u5bc6\u7801\uff1a" + r(14) + "8"):
        fs = rules.find(text)
        assert fs, text


def test_the_secret_value_is_replaced_but_the_name_stays_readable():
    text = f"export MAILGUN_API_KEY=key-{r(32, HEX)}\nexport DEBUG=true"
    fs = rules.find(text)
    assert fs and all(text[f.start:f.end].startswith("key-") for f in fs)


# --- personal data -----------------------------------------------------------------------------

def _cn_id():
    body = rnd.choice(["110105", "310104", "440305", "510107", "330106"]) + str(rnd.randint(1960, 2003)) + f"{rnd.randint(1, 12):02d}{rnd.randint(1, 28):02d}{rnd.randint(100, 999)}"
    w = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
    return body + "10X98765432"[sum(int(c) * k for c, k in zip(body, w)) % 11]


def _cn_mobile():
    return "1" + rnd.choice("358") + str(rnd.randint(10 ** 8, 10 ** 9 - 1))


PII = []   # (text, [(span, entity-kind)])
for name, addr in [("\u5f20\u4f1f", "\u5317\u4eac\u5e02\u671d\u9633\u533a\u5efa\u56fd\u8def88\u53f7\u96623\u53f7\u697c1201\u5ba4"), ("\u674e\u79c0\u82f1", "\u4e0a\u6d77\u5e02\u6d66\u4e1c\u65b0\u533a\u4e16\u7eaa\u5927\u9053100\u53f7"), ("\u738b\u5efa\u56fd", "\u5e7f\u4e1c\u7701\u6df1\u5733\u5e02\u5357\u5c71\u533a\u79d1\u6280\u56ed\u5357\u533a\u9ad8\u65b0\u5357\u4e00\u90535\u680b302"),
                   ("\u6b27\u9633\u660e\u8f69", "\u56db\u5ddd\u7701\u6210\u90fd\u5e02\u6b66\u4faf\u533a\u4eba\u6c11\u5357\u8def\u56db\u6bb527\u53f7"), ("\u5218\u82b3", "\u6d59\u6c5f\u7701\u676d\u5dde\u5e02\u897f\u6e56\u533a\u6587\u4e09\u8def398\u53f7")]:
    i, m = _cn_id(), _cn_mobile()
    PII.append((f"\u623f\u5c4b\u79df\u8d41\u5408\u540c\n\u7532\u65b9\uff08\u51fa\u79df\u4eba\uff09\uff1a{name}\n\u8eab\u4efd\u8bc1\u53f7\uff1a{i}\n\u4f4f\u5740\uff1a{addr}\n\u8054\u7cfb\u7535\u8bdd\uff1a{m}\n\u4e59\u65b9\uff08\u627f\u79df\u4eba\uff09\uff1a\u89c1\u9644\u4ef6",
                [(name, "PERSON"), (i, "ID"), (addr, "ADDRESS"), (m, "PHONE")]))
PII += [
    ("\u672c\u4eba\u5468\u6770\uff0c\u8eab\u4efd\u8bc1\u53f7\u7801" + (zi := _cn_id()) + "\uff0c\u73b0\u4f4f\u6e56\u5317\u7701\u6b66\u6c49\u5e02\u6d2a\u5c71\u533a\u73de\u55bb\u8def1037\u53f7\uff0c\u81ea\u613f\u7b7e\u7f72\u672c\u534f\u8bae\u3002", [("\u5468\u6770", "PERSON"), (zi, "ID"), ("\u6e56\u5317\u7701\u6b66\u6c49\u5e02\u6d2a\u5c71\u533a\u73de\u55bb\u8def1037\u53f7", "ADDRESS")]),
    ("\u6536\u8d27\u5730\u5740\uff1a\u5317\u4eac\u5e02\u6d77\u6dc0\u533a\u4e2d\u5173\u6751\u5927\u88571\u53f7\u6d77\u9f99\u5927\u53a62\u5c42", [("\u5317\u4eac\u5e02\u6d77\u6dc0\u533a\u4e2d\u5173\u6751\u5927\u88571\u53f7\u6d77\u9f99\u5927\u53a62\u5c42", "ADDRESS")]),
    ("\u5458\u5de5\u674e\u660e\uff08\u5de5\u53f7E10234\uff09\u5bb6\u5ead\u4f4f\u5740\u4e3a\u4e0a\u6d77\u5e02\u5f90\u6c47\u533a\u6f15\u6eaa\u5317\u8def88\u53f7", [("\u674e\u660e", "PERSON"), ("\u4e0a\u6d77\u5e02\u5f90\u6c47\u533a\u6f15\u6eaa\u5317\u8def88\u53f7", "ADDRESS")]),
    ("\u8bf7\u8054\u7cfb\u9648\u5148\u751f\uff0c\u7535\u8bdd" + (cm := _cn_mobile()), [("\u9648", "PERSON"), (cm, "PHONE")]),
    ('SERVICE AGREEMENT\nThis agreement is made between John Smith ("Client"), residing at 1428 Elm Street, Springfield, IL 62704, and Maria Gonzalez ("Provider").',
     [("John Smith", "PERSON"), ("Maria Gonzalez", "PERSON"), ("1428 Elm Street, Springfield, IL 62704", "ADDRESS")]),
    ("Party A: Elena Vasquez\nAddress: 4521 Oak Ridge Drive, Austin, TX 78745\nSSN: 078-05-1120\nDate of birth: 03/14/1985",
     [("Elena Vasquez", "PERSON"), ("4521 Oak Ridge Drive, Austin, TX 78745", "ADDRESS")]),
    ("Tenant: Tom Becker, passport no. X1234567, home address 18 Maple Lane, Portland, OR 97201",
     [("Tom Becker", "PERSON"), ("X1234567", "ID"), ("18 Maple Lane, Portland, OR 97201", "ADDRESS")]),
    ("Employee: Anna Kowalski. National Insurance number QQ123456C. Lives at Flat 3, 12 High Street, Leeds LS1 4AP.",
     [("Anna Kowalski", "PERSON"), ("QQ123456C", "ID"), ("Flat 3, 12 High Street, Leeds LS1 4AP", "ADDRESS")]),
    ("Signed: ______ Raj Patel   PAN: ABCDE1234F   Aadhaar: 2345 6789 0123   Address: 14 MG Road, Bengaluru 560001",
     [("Raj Patel", "PERSON"), ("ABCDE1234F", "ID"), ("2345 6789 0123", "ID"), ("14 MG Road, Bengaluru 560001", "ADDRESS")]),
    ("Taiwan resident Lin Yu-Chen, ID A123456789, address 100 Zhongxiao East Road, Taipei 10041",
     [("Lin Yu-Chen", "PERSON"), ("A123456789", "ID"), ("100 Zhongxiao East Road, Taipei 10041", "ADDRESS")]),
    ("Please ship it to Karen Whitfield, 4 Birch Court, Denver CO 80203.", [("Karen Whitfield", "PERSON"), ("4 Birch Court, Denver CO 80203", "ADDRESS")]),
    ("Vertrag zwischen Hans Müller, wohnhaft Hauptstraße 12, 10115 Berlin, und der Gegenseite", [("Hauptstraße 12, 10115 Berlin", "ADDRESS")]),
    ("Contract between Sofia Petrova and Omar Haddad, 9 Rue de la Paix, 75002 Paris", [("Sofia Petrova", "PERSON"), ("Omar Haddad", "PERSON"), ("9 Rue de la Paix, 75002 Paris", "ADDRESS")]),
]
PII += [   # a second batch written after the first round of fixes, to see what generalizes
    ("Borrower: Jonathan Reeves, DOB 1979-02-11, residing at 62 Linden Avenue, Apt 5C, Columbus, OH 43215.",
     [("Jonathan Reeves", "PERSON"), ("62 Linden Avenue, Apt 5C, Columbus, OH 43215", "ADDRESS")]),
    ("Employee Name: Mei-Ling Chang\nHome Address: 8 Harbour View Road, Hong Kong", [("Mei-Ling Chang", "PERSON"), ("8 Harbour View Road, Hong Kong", "ADDRESS")]),
    ("Licensee: Carlos Fernández, driver's licence no. D1234567890 expires 2029", [("Carlos Fernández", "PERSON"), ("D1234567890", "ID")]),
    ("Mr. Peter Hallworth (NI number JG103759A) lives at 5 Station Road, Cambridge CB1 2JD", [("JG103759A", "ID"), ("5 Station Road, Cambridge CB1 2JD", "ADDRESS")]),
    ("Please send the contract to Sarah O'Connell, 14 Rosewood Crescent, Dublin D04 X2Y3", [("Sarah O'Connell", "PERSON"), ("14 Rosewood Crescent, Dublin D04 X2Y3", "ADDRESS")]),
    ("Customer: David Mwangi\nDelivery address: 27 Moi Avenue, Nairobi 00100\nPhone on file", [("David Mwangi", "PERSON"), ("27 Moi Avenue, Nairobi 00100", "ADDRESS")]),
    ("\u4e59\u65b9\uff1a\u8d75\u4e3d\u9896\uff0c\u5c45\u6c11\u8eab\u4efd\u8bc1\u53f7\u7801\uff1a" + (zid := _cn_id()), [("\u8d75\u4e3d\u9896", "PERSON"), (zid, "ID")]),
    ("\u5bb6\u5ead\u4f4f\u5740\uff1a\u5e7f\u4e1c\u7701\u5e7f\u5dde\u5e02\u5929\u6cb3\u533a\u4f53\u80b2\u897f\u8def103\u53f7\u7ef4\u591a\u5229\u5e7f\u573aB\u5ea72305\u5ba4", [("\u5e7f\u4e1c\u7701\u5e7f\u5dde\u5e02\u5929\u6cb3\u533a\u4f53\u80b2\u897f\u8def103\u53f7\u7ef4\u591a\u5229\u5e7f\u573aB\u5ea72305\u5ba4", "ADDRESS")]),
    ("\u8054\u7cfb\u4eba\uff1a\u5b59\u5c0f\u7ea2 \u7535\u8bdd\uff1a13912345678", [("\u5b59\u5c0f\u7ea2", "PERSON"), ("13912345678", "PHONE")]),
    ("\u6536\u4ef6\u4eba\uff1a\u5434\u5929\u5b87 \u5730\u5740\uff1a\u91cd\u5e86\u5e02\u6e1d\u4e2d\u533a\u89e3\u653e\u7891\u6c11\u6743\u8def28\u53f7", [("\u5434\u5929\u5b87", "PERSON"), ("\u91cd\u5e86\u5e02\u6e1d\u4e2d\u533a\u89e3\u653e\u7891\u6c11\u6743\u8def28\u53f7", "ADDRESS")]),
]
PII_CASES = [pytest.param(t, sp, id=f"pii{i}") for i, (t, sp) in enumerate(PII)]


@pytest.mark.parametrize("text,spans", PII_CASES)
def test_contract_style_personal_data_is_found(text, spans):
    fs = rules.find(text)
    for span, kind in spans:
        a = text.index(span)
        b = a + len(span)
        cover = sum(max(0, min(f.end, b) - max(f.start, a)) for f in fs)
        assert cover >= 0.8 * len(span), (kind, span, [(text[f.start:f.end], f.entity) for f in fs])


def test_entities_are_typed_for_placeholders():
    text = "Party A: Elena Vasquez\nAddress: 4521 Oak Ridge Drive, Austin, TX 78745\nPAN: ABCDE1234F"
    kinds = {f.entity for f in rules.find(text)}
    assert {"PERSON", "ADDRESS", "NATIONAL_ID"} <= kinds
    assert "CN_ID_CARD" in {f.entity for f in rules.find("\u8eab\u4efd\u8bc1\u53f7\uff1a" + _cn_id())}
    assert "CN_MOBILE" in {f.entity for f in rules.find("\u624b\u673a" + _cn_mobile())}


def test_a_chinese_id_number_needs_a_valid_checksum():
    bad = _cn_id()[:-1] + ("0" if _cn_id()[-1] != "0" else "1")
    ok = _cn_id()
    assert not [f for f in rules.find("\u7f16\u53f7 " + bad + " \u7ed3\u675f") if f.entity == "CN_ID_CARD"] or bad == ok
    assert [f for f in rules.find("\u7f16\u53f7 " + ok + " \u7ed3\u675f") if f.entity == "CN_ID_CARD"]


# --- things that must NOT be touched -----------------------------------------------------------

CLEAN = [
    "primary_key = True",
    "monkey = 'banana'",
    "keyboard_layout = 'qwertyuiop'",
    "author = 'Jane Doe'",
    "token: Optional[str] = None",
    "api_key = os.environ.get('OPENAI_API_KEY')",
    "secret_key = settings.SECRET_KEY",
    "password = request.form['password']",
    "export OPENAI_API_KEY=$OPENAI_API_KEY",
    "OPENAI_API_KEY=${OPENAI_API_KEY}",
    "OPENAI_API_KEY=<SECRET_1>",
    "password=<SECRET_2>",
    "ssh_key_path = '/home/user/.ssh/id_rsa'",
    "token_type = 'bearer'",
    "def get_token(self, secret_name: str) -> str:",
    '"integrity": "sha512-' + base64.b64encode(rnd.randbytes(64)).decode() + '"',
    "commit " + r(40, HEX) + " Merge branch 'main'",
    "id = '3f2b8c1e-9d4a-4e6b-8a1f-2c7d5e9b0a13'",
    "data:image/png;base64," + base64.b64encode(rnd.randbytes(60)).decode(),
    "Address: str",
    "Address: Optional[str] = None",
    "IP address: 10.0.0.1",
    "email address: someone@example.com",
    "\u7532\u65b9\uff1a\u5317\u4eac\u67d0\u67d0\u79d1\u6280\u6709\u9650\u516c\u53f8",
    "Customer Service Representative: please escalate",
    "Name: str = Field(...)",
    "Contact: support@example.com",
    "\u540c\u4e8b\u738b\u82b3\u660e\u5929\u8bf7\u5047\uff0c\u4e0d\u7528\u7b49\u5979",
    "\u4f4f\u5740\uff1a\u89c1\u9644\u4ef6",
    "\u5730\u5740\uff1a\u5f85\u5b9a",
    "The quick brown fox jumps over the lazy dog. Contact support for help.",
    "postgresql://user:<password>@localhost:5432/db",
    "def authenticate(user, password):\n    return check(user, password)",
    "git log --oneline | head -5",
    "version = '2.14.1'  # released 2026-10-09",
    "See https://example.com/docs/getting-started for details",
    "x = 12345678901234567",
    "retry_count = 5\nkey_length = 4096",
]


@pytest.mark.parametrize("text", CLEAN)
def test_ordinary_text_is_left_alone(text):
    assert rules.find(text) == [], [(text[f.start:f.end], f.entity) for f in rules.find(text)]


# --- passwords with punctuation, wordy passwords, long encoded secrets (review 2026-10-15) ---------

PUNCT = "#;)(,:'<>[]{}\\"


def _punct_password():
    chars = [rnd.choice(ALNUM) for _ in range(14)]
    for i in (2, 5, 9):
        chars[i] = rnd.choice(PUNCT.replace("'", "").replace("\\", ""))
    return "".join(chars)


@pytest.mark.parametrize("shape", ["env", "export", "env_quoted", "json", "python", "yaml_quoted"])
def test_a_password_with_punctuation_is_hidden_whole(shape):
    v = _punct_password()
    text = {"env": f"DB_PASSWORD={v}", "export": f"export DB_PASSWORD={v}", "env_quoted": f'DB_PASSWORD="{v}"',
            "json": '{"db_password": "' + v + '"}', "python": f"db_password = '{v}'",
            "yaml_quoted": f'db_password: "{v}"'}[shape]
    found = rules.find(text)
    assert _covered(found, text, v), (text, [(text[f.start:f.end]) for f in found])


@pytest.mark.parametrize("text", ['{"password": "Password123"}', "password: Sunshine2024", 'ADMIN_PASS="Welcome2026"'])
def test_a_wordy_password_under_a_password_name_is_hidden(text):
    assert rules.find(text), text


def test_a_long_encoded_secret_under_a_neutral_key_is_hidden():
    v = base64.b64encode(rnd.randbytes(300)).decode()
    text = '{"auths": {"registry.example.com": {".dockerconfigjson": "' + v + '"}}}'
    assert _covered(rules.find(text), text, v)


@pytest.mark.parametrize("text", [
    "key = config['api_key_v2']",
    'token = "Bearer {token}"',
    'password = "Enter your password"',
    'PASSWORD="$(cat secret.txt)"',
    "password=get_pw(x2)",
    "pass_through: identity_v2",
])
def test_code_around_password_names_is_left_alone(text):
    assert rules.find(text) == [], [(text[f.start:f.end], f.entity) for f in rules.find(text)]


@pytest.mark.parametrize("unit", ["a-", "a.", "a+", "ab-cd."])
def test_long_runs_of_scheme_characters_scan_in_linear_time(unit):
    import time
    text = unit * (48 * 1024 // len(unit))
    t = time.perf_counter()
    rules.find(text)
    assert time.perf_counter() - t < 0.5


def test_url_credentials_still_found_after_scheme_characters():
    pw = r(16)
    text = f"a-b.c+d postgres+psycopg://admin:{pw}@db.example.com/prod"
    assert any(text[f.start:f.end] == pw for f in rules.find(text))


# --- CredData dev-split false alarms (2026-10-18) ---------------------------------------------------

@pytest.mark.parametrize("text", [
    "{'Authorization': 'Basic login_and_password_removed', 'User-Agent': 'PyGithub/Python'}",
    'type="System.Boolean, mscorlib, Version=4.0.0.0, Culture=neutral, PublicKeyToken=' + r(16, HEX) + '"',
    '"public-key": "' + r(64, HEX) + '",',
    "public_key = '" + r(44, B64) + "'",
    "PrivateKey=ALICE_cf_prime239v2_PUB",
    "PeerKey=KAS-ECC-CDH_K-571_C2-Peer-PUBLIC",
    "PrivPubKeyPair = BOB_wap-wsg-idm-ecid-wtls6:BOB_wap-wsg-idm-ecid-wtls6_PUB",
    "STATUS_NO_TOKEN = 0xC000007C,",
    "EC_R_PEER_KEY_ERROR:149:peer key error",
    '"harvester-token-checksum": "' + r(64, HEX) + '",',
    "sqlalchemy.url = driver://user:pass@localhost/dbname",
    '{"https://:password@example.net",',
    "curl http://admin:admin@localhost:3000/api/org",
])
def test_creddata_false_alarm_shapes_are_left_alone(text):
    assert rules.find(text) == [], [(text[f.start:f.end], f.entity) for f in rules.find(text)]


@pytest.mark.parametrize("text", [
    "Authorization: Basic " + base64.b64encode(("admin:" + r(12)).encode()).decode(),
    "private_key = '" + r(44, B64) + "'",
    "APP_KEY=base64:" + r(43, B64) + "=",
    "postgres://app:" + r(10) + "@db.internal/prod",
])
def test_real_shapes_next_to_those_false_alarms_are_still_found(text):
    assert rules.find(text), text


@pytest.mark.parametrize("text", [
    "'PASSWORD': '123456',",
    "'password': 'admin',",
    '"LABEL_CURRENT_PASSWORD": "現有密碼",',
    '"FORGOT_PASSWORD": "Forgot password?",',
    "password: Hasło",
])
def test_stand_in_passwords_and_ui_text_are_left_alone(text):
    assert rules.find(text) == [], [(text[f.start:f.end], f.entity) for f in rules.find(text)]


@pytest.mark.parametrize("text", ['DB_PASSWORD: "kxdumskx"', "password: 'x-pack-test-password'", 'password = "tftest"',
                                  "MAILER_PASS=super-secret-password", 'MYSQL_ROOT_PASSWORD="rootpass"', "password = 'qn-wcpd'", "POSTGRES_PASSWORD: ombzfz",
                                  'password = Some("owlknh"),', "Password = tkeftvoh",
                                  'Password: types.StringValue("qdspcg"),', 'Password: SecretStr("hcctgcx"),'])
def test_a_plain_word_password_under_a_password_name_is_hidden(text):
    assert rules.find(text), text


# --- CredData dev-split misses (2026-10-18) ---------------------------------------------------------

B32 = string.ascii_uppercase + "234567"


def _found(text, core):
    return _covered(rules.find(text), text, core)


MISSED = [  # (text, part that must be hidden)
    ("SU" + r(56, B32), None),
    ('const string accSeed = "SA' + r(56, B32) + '";', None),
    ("Authorization: NTLM TlRMTVNTUAADAAAA" + r(80, B64), None),
    ('"value": "OAuth oauth_consumer_key=\\"' + r(16) + '\\",oauth_signature_method=\\"HMAC-SHA1\\",oauth_nonce=\\"x\\""', "KEY"),
    ("'oauth_verifier: '" + r(11) + "',", "VERIFIER"),
    ('curl_easy_setopt(curl, CURLOPT_PROXYPASSWORD, "' + r(6, string.ascii_lowercase) + '");', None),
    ('curl_easy_setopt(curl, CURLOPT_USERPWD, "' + r(5, string.ascii_lowercase) + ":" + r(6, string.ascii_lowercase) + '");', None),
    ("CREATE ROLE gitlab with LOGIN CREATEDB PASSWORD '" + r(8, string.ascii_lowercase) + "';", None),
    ("CREATE USER 'app'@'localhost' IDENTIFIED BY '" + r(9, string.ascii_lowercase) + "';", None),
    ("CREATE ROLE app_admin WITH LOGIN PASSWORD " + r(10, string.ascii_lowercase) + ";", None),
    ("nats server report jetstream --user admin --password " + r(6) + "!", None),
    ("--username spo@corp.local --password '" + r(8, string.ascii_lowercase) + "157!' \\", None),
    ("$obj = Get-NtToken -Logon -User Bob -Password " + r(10), None),
    ("mysql -u root -p" + r(8, string.ascii_lowercase) + " -e 'SELECT 1'", None),
    ("$pw = ConvertTo-SecureString '" + r(12) + "' -AsPlainText -Force", None),
    ("Ctrl.IKM = hexkey:" + r(64, HEX), None),
    ("-macopt hexkey:" + r(32, HEX).upper() + " \\", None),
    ("Ctrl.hexsecret = hexsecret:" + r(48, HEX), None),
    ('"unique_id": "AROA' + r(17, UP) + '"', None),
    ("'secret' => '" + r(12, string.ascii_lowercase) + "',", None),
    ("consumer_secret: '" + r(3, string.ascii_lowercase) + r(3, string.digits) + "',", None),
    ('"credential_value": "' + r(50, string.ascii_lowercase) + '",', None),
    ("clientSecret: '" + r(12, string.ascii_lowercase) + "'", None),
    ('token: "' + r(7, string.ascii_lowercase + string.digits) + "_" + r(5, string.ascii_lowercase + string.digits) + '",', None),
    ("test_token = '" + r(60, string.ascii_lowercase + string.digits) + "TROQ'", None),
]


@pytest.mark.parametrize("text,core", MISSED, ids=[f"miss{i}" for i in range(len(MISSED))])
def test_creddata_missed_shapes_are_found(text, core):
    if core in ("KEY", "VERIFIER"):
        core = text.split("=\\\"" if core == "KEY" else ": '")[1].split("\\\"" if core == "KEY" else "'")[0]
    assert rules.find(text), text
    if core:
        assert _found(text, core), text


@pytest.mark.parametrize("text", [
    "oauth_signature_method: 'HMAC-SHA256',",
    "token_type = 'bearer'",
    "auth_method: 'password'",
    "token: 'refresh'",
    "secret: SECRET_KEY",
    "--password $DB_PASSWORD",
    "mysql -u root -p -e 'SELECT 1'",
    "ALTER USER x PASSWORD EXPIRE;",
])
def test_neighbours_of_the_missed_shapes_are_left_alone(text):
    assert rules.find(text) == [], [(text[f.start:f.end], f.entity) for f in rules.find(text)]


@pytest.mark.parametrize("text", [
'ClientSecret = "ClientSecret"', 'authID:      "myAuthID",', "secret: SECRET_KEY_BASE",
    "oauth_token: OTOKEN",
    'easy_setopt(curl, CURLOPT_USERPWD, "xxx:yyy");', 'easy_setopt(curl, CURLOPT_USERPWD, "foo:bar");',
    "-password (required) Optional if -force is specified", "--password=NATS_PASSWORD  Password",
    "-W, --password             Force password prompt.", "CREATE USER myuser WITH PASSWORD 'password';",
    'accessTokenParameter="?access_token=$HelixApiAccessToken"',
])
def test_example_values_in_the_new_shapes_are_left_alone(text):
    assert rules.find(text) == [], [(text[f.start:f.end], f.entity) for f in rules.find(text)]


# --- base64 blocks and private key headers in prose (2026-10-18) -------------------------------------

def _b64_lines(n_lines, first=""):
    body = first + base64.b64encode(rnd.randbytes(48 * n_lines)).decode()
    return [body[i:i + 64] for i in range(0, 64 * n_lines, 64)]


@pytest.mark.parametrize("wrap", ["plain", "java_string", "indented"])
def test_a_multi_line_base64_block_is_encoded_data_not_a_secret(wrap):
    lines = _b64_lines(8)
    fmt = {"plain": "{}", "java_string": '"{}\\n" +', "indented": "    {}"}[wrap]
    text = "-----BEGIN CERTIFICATE-----\n" + "\n".join(fmt.format(x) for x in lines) + "\n-----END CERTIFICATE-----\n"
    assert rules.find(text) == [], [text[f.start:f.end][:40] for f in rules.find(text)]


def test_a_base64_private_key_body_without_armor_is_still_found():
    lines = _b64_lines(8, first="MIIEowIBAAKCAQEA")
    text = "private:\n" + "\n".join(lines) + "\n"
    assert rules.find(text)


def test_a_single_high_entropy_token_is_still_found():
    v = r(48)
    assert _found(f"x {v} y", v)


def test_a_pgp_private_key_block_is_found_whole():
    body = "\n".join(_b64_lines(6))
    text = f"-----BEGIN PGP PRIVATE KEY BLOCK-----\n\n{body}\n-----END PGP PRIVATE KEY BLOCK-----\n"
    assert _found(text, body)


def test_a_private_key_header_named_in_prose_does_not_swallow_the_file():
    text = ('* PKCS#1 PEM (starts with "-----BEGIN RSA PRIVATE KEY-----")\n'
            "* PKCS#8 PEM\n\nSystem.out.println(new GsonBuilder().create());\n" + "Some more prose here.\n" * 50)
    assert all(f.end - f.start < 80 for f in rules.find(text)), [f.end - f.start for f in rules.find(text)]


def test_an_unterminated_private_key_keeps_its_body():
    body = "\n".join(_b64_lines(5))
    text = f"-----BEGIN RSA PRIVATE KEY-----\n{body}\n"
    assert _found(text, body)


def test_a_password_variable_passed_on_is_left_alone():
    assert rules.find("    password: password,\n    user: user,") == []


@pytest.mark.parametrize("text", [
    "Key = 00000000000000000000000000000000",
    "config.oauth_token_secret = '0123456789'",
    '# secret = "00000000000000000000000000000000"',
    'password: "$argon2id$v=19$m=131072,p=8$' + r(16) + "$" + r(43, B64) + '"',
    'ok, err := CheckPassword("password", "$6$rounds=abc$' + r(16) + "$" + r(86, B64.replace("+", ".")) + '")',
    '{"node_id":"MDM6VGFnMjc5MDE1Mjg2OjJkYmFhMDdhNmUzY2FkNjE0ZTAyNDEwZjVkOWVmNjM5NjFkNjFkYTg=","sha":"x"}',
    '"ThumbnailPicture": "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAQCAwMDAgQDAwMEBAQEBQkGBQUFBQsICAYJDQsNDQ0LDAwOEBQRDg8T"',
    "pass: PassEvent,",
])
def test_repetitive_example_hash_and_encoded_file_values_are_left_alone(text):
    assert rules.find(text) == [], [(text[f.start:f.end], f.entity) for f in rules.find(text)]


def test_every_value_of_the_demo_env_file_is_found():
    """The README demo: an agent reads demo/.env. Its fake values describe themselves as fake; they must still go."""
    from pathlib import Path
    for line in (Path(__file__).resolve().parent.parent / "demo" / ".env").read_text().splitlines():
        if "=" in line:
            value = line.split("=", 1)[1]
            core = value.split("://")[1].split("@")[0].split(":")[1] if "://" in value else value
            assert _found(line, core), line


MISSED_2 = [
    "  - TEST_RABBITMQ_URL=amqp://guest:" + "ivive" + "@rabbitmq:5672/",
    "EMAIL_SERVER=smtps://user@gmail.com:" + r(8, string.ascii_lowercase) + "@smtp.gmail.com:465",
    "heroku config:set SMTP_PASSWORD=" + r(12, string.ascii_lowercase),
    "INWX_PASSWORD=" + r(10, string.ascii_lowercase) + " \\",
    "  - GW_SERVER_JWT_SECRET=" + r(10, string.ascii_lowercase),
    "password=" + r(6, string.ascii_uppercase),
    "#     http://rt.example.org/Ticket/Display.html?id=2582&user=guest&pass=" + r(5, string.ascii_lowercase),
    'setPassword("' + r(5, string.ascii_lowercase) + '");',
    'details.setClientSecret("' + r(32, HEX) + '");',
    "key = unhexlify('" + r(64, HEX) + "')",
    'var keyData = hexStringToUint8Array("' + r(32, HEX) + '");',
    "Ctrl.hexsalt = hexsalt:" + r(8, HEX),
    "-out server.p12 -name cert -password pass:" + r(8, string.ascii_lowercase),
    'Salt = "' + r(36, string.ascii_letters) + '"',
]


@pytest.mark.parametrize("text", MISSED_2, ids=[f"miss2_{i}" for i in range(len(MISSED_2))])
def test_more_creddata_missed_shapes_are_found(text):
    assert rules.find(text), text


@pytest.mark.parametrize("text", ["x=foo bar", "PATH=/usr/bin:$PATH", "DEBUG=true", "salt: true", "pass: true",
                                  "setPassword(password);", "MODE=production"])
def test_neighbours_of_those_shapes_are_left_alone(text):
    assert rules.find(text) == [], [(text[f.start:f.end], f.entity) for f in rules.find(text)]


@pytest.mark.parametrize("text", [
    "$result = $this->jwtAuth->setToken('foo.bar.baz')->refresh();", 'Credential credential = new Credential("/captain");',
    "JOKER_PASSWORD=<your password> \\", "self.__pwd = 'C:\\\\'", "details.HashedPassword = hash",
    "-newkeypass,pass:,,,BLANK,,BLANK,,BLANK", "-kdfopt pass:passwordPASSWORDpassword -kdfopt salt:saltSALT",
])
def test_neighbours_of_setters_and_env_values_are_left_alone(text):
    assert rules.find(text) == [], [(text[f.start:f.end], f.entity) for f in rules.find(text)]


@pytest.mark.parametrize("text", [
    "Key = 000102030405060708090A0B0C0D0E0F101112131415161718191A1B",
    "Key = 00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff",
    "Key = ef012345ef012345ef012345ef012345",
    '"key":  "-----BEGIN PUBLIC KEY-----\\n' + r(64, B64) + "\\n" + r(64, B64) + '\\n-----END PUBLIC KEY-----\\n",',
    '"cert": "-----BEGIN CERTIFICATE-----\\n' + r(64, B64) + "\\n" + r(64, B64) + '\\n-----END CERTIFICATE-----"',
    "<!-- cache-key: cdktf-0.20.8 input-" + r(64, HEX) + " -->",
    'TopologyKey:       "k8s.io/zone",',
    '- match: { aggregations.histo.buckets.0.key_as_string: "2017-01-01T05:00:00.000Z" }',
])
def test_test_vectors_public_armor_and_key_names_are_left_alone(text):
    assert rules.find(text) == [], [(text[f.start:f.end][:50], f.entity) for f in rules.find(text)]


def test_a_real_hex_key_under_a_key_name_is_still_found():
    v = r(64, HEX)
    assert _found(f"Key = {v}", v) and _found(f"aes_key = '{v}'", v)


def test_prose_after_password_is_is_not_a_password():
    text = ('"getterDocumentation" : "/**<p>The identifier of the user for whom the password is reset.</p>\\n'
            '@return The identifier of the user for whom the password is reset.*/",')
    assert rules.find(text) == [], [text[f.start:f.end] for f in rules.find(text)]
    assert rules.find("the password is " + r(10) + "7x for the staging db")


# --- review of the CredData tuning (2026-10-18): recall regressions and placeholder damage ------------

def _pem_text():
    body = base64.b64encode(rnd.randbytes(900)).decode()
    lines = ["MIIEowIBAAKCAQEA" + body[16:64]] + [body[i:i + 64] for i in range(64, len(body), 64)]
    return "-----BEGIN RSA PRIVATE KEY-----\n" + "\n".join(lines) + "\n-----END RSA PRIVATE KEY-----\n", lines


def _share_covered(text, lines):
    found = rules.find(text)
    return sum(_covered(found, text, x) for x in lines) / len(lines)


def test_a_base64_wrapped_private_key_is_found():
    pem, _ = _pem_text()
    wrapped = base64.encodebytes(pem.encode()).decode().splitlines()
    assert _share_covered("tls.key: |\n" + "\n".join("  " + x for x in wrapped), wrapped) >= 0.8


def test_a_base64_wrapped_service_account_is_found():
    import json
    pem, _ = _pem_text()
    sa = json.dumps({"type": "service_account", "private_key_id": r(40, HEX), "private_key": pem}, indent=2)
    wrapped = base64.encodebytes(sa.encode()).decode().splitlines()
    assert _share_covered("echo '\n" + "\n".join(wrapped) + "' | base64 -d", wrapped) >= 0.8


def test_private_key_lines_without_their_header_are_found():
    """What the entropy rule finds on one line alone, it still finds in a headless key body."""
    _, lines = _pem_text()
    alone = [x for x in lines if rules.find(x)]
    assert len(alone) >= len(lines) - 2
    mid, tail = lines[3:10], lines[-8:]
    assert _share_covered("\n".join(mid), [x for x in mid if x in alone]) == 1
    assert _share_covered("\n".join(tail) + "\n-----END RSA PRIVATE KEY-----", [x for x in tail if x in alone]) == 1


def test_a_list_of_keys_one_per_line_is_found():
    """What the entropy rule finds on its own line, it still finds in a list (a list is not an encoded file)."""
    keys = [k for k in (r(40, B64) for _ in range(20)) if rules.find(k)][:5]
    assert _share_covered("API_KEYS = [\n" + ",\n".join(f'  "{k}"' for k in keys) + "\n]", keys) == 1
    toks = [k for k in (r(44) for _ in range(20)) if rules.find(k)][:5]
    assert _share_covered("\n".join(toks), toks) == 1


@pytest.mark.parametrize("first", ["MIIC4DCCAcqgAwIBAgIBATALBgkqhkiG9w0BAQswIzEh", "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE",
                                   "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA", "JVBERi0xLjQKJcOkw7zDtsOfCjIgMCBvYmoKPDwv"])
def test_a_certificate_public_key_or_file_block_without_armor_is_left_alone(first):
    lines = _b64_lines(8, first=first)
    assert rules.find("\n".join(lines)) == []


def test_a_mail_attachment_block_is_left_alone():
    text = "Content-Type: application/octet-stream\nContent-Transfer-Encoding: base64\n\n" + "\n".join(_b64_lines(10))
    assert rules.find(text) == []


@pytest.mark.parametrize("text,core", [
    ('password: "$uperSecret99"', "$uperSecret99"), ('password="%Kq7Lm2xP9q"', "%Kq7Lm2xP9q"),
    ('password: "@Kq7Lm2xP9q"', "@Kq7Lm2xP9q"), ('password: "C:\\\\Kq7Lm2xP9q"', None),
    ("postgres://admin:admin2024@prod-db.internal/app", "admin2024"), ("mysql://root:root123@10.0.0.5/db", "root123"),
    ("curl -u admin:test1234 https://api.internal", "test1234"), ("spring.datasource.password=root1234", "root1234"),
    ("DB_PASSWORD=admin123", "admin123"), ("redis://:password123@host:6379", "password123"),
    ("API_KEY=live_9f8e7d6c5b4a3f2e1d0c", "live_9f8e7d6c5b4a3f2e1d0c"), ('api_key = "prod-key-7f3a-9b21"', "prod-key-7f3a-9b21"),
    ('API_KEY="acme-1a558e9e-6fe3110f"', "acme-1a558e9e-6fe3110f"), ('OPENAI_API_KEY="abcd1234efgh"', "abcd1234efgh"),
    ("SESSION_SECRET=KeyboardCatRocks", "KeyboardCatRocks"), ("pass_phrase = 'hunter2hunter'", "hunter2hunter"),
])
def test_review_recall_cases_are_found(text, core):
    assert rules.find(text), text
    if core:
        assert _found(text, core), text


@pytest.mark.parametrize("text", [
    "DB_PASSWORD=$DB_PASS", 'password: "${DB_PASSWORD}"', "JOKER_PASSWORD=<your password>", "password=%DB_PASSWORD%",
    "password = @password", '{"token": "identifier"}', 'Token { token: Some("whitespace") }', 'new Token("NUMBER")',
    'this.setToken("loading")', "--password TEXT", "  [--password PASS]", '{"auth": "oauth2"}', "token = tok123",
    'token = parse("2024-01-01")', "PASSWORD 'varchar'", 'pass = "skip"', '{"password": "Passwort"}',
])
def test_review_placeholder_damage_cases_are_left_alone(text):
    assert rules.find(text) == [], [(text[f.start:f.end], f.entity) for f in rules.find(text)]


@pytest.mark.parametrize("text", [" " * 48 * 1024, ("\t" + " " * 300 + "x\n") * 160, (" " * 2000 + "x\n") * 24,
                                  "-----BEGIN PRIVATE KEY-----\n" * 1700, "-----BEGIN CERTIFICATE-----\n" * 1700])
def test_whitespace_and_repeated_headers_scan_in_linear_time(text):
    import time
    t = time.perf_counter()
    rules.find(text)
    assert time.perf_counter() - t < 0.5


@pytest.mark.parametrize("text,core", [
    ("The token is SummerTime2024", "SummerTime2024"), ("aws_secret_access_key = " + r(40, string.ascii_lowercase), None), ("API_KEY=0xDEADBEEFCAFE1234", "0xDEADBEEFCAFE1234"),
    ("password=aaaabbbb1", "aaaabbbb1"), ("password: abc123abc123", "abc123abc123"),
])
def test_weak_but_real_values_are_found(text, core):
    assert _found(text, core or text.split(" = ")[1]), [(text[f.start:f.end]) for f in rules.find(text)]


# --- names in tables: a name column (by its header) makes every row's cell a person

ZH_NAME_A, ZH_NAME_B = "方静怡", "覃俊杰"   # two invented Chinese names
ZH_HEADER = "姓名"                                       # "name"


def _persons(text):
    return {text[f.start:f.end] for f in rules.find(text) if f.entity == "PERSON"}


def test_names_in_a_csv_name_column_are_found():
    text = ("name,email,department,salary\n"
            "Isabel Brennan,ib@example.com,Sales,53200\n"
            f"{ZH_NAME_A},fj@example.com,Finance,61000\n"
            "Daniel Fairbanks,df@example.com,Legal,161200\n")
    assert {"Isabel Brennan", ZH_NAME_A, "Daniel Fairbanks"} <= _persons(text)


def test_quoted_cells_and_a_name_column_that_is_not_first():
    text = ('id,full_name,address\n'
            '7,"Clara Lindqvist","4401 Bramble Street, Springfield"\n'
            f'8,{ZH_NAME_B},"x"\n')
    assert {"Clara Lindqvist", ZH_NAME_B} <= _persons(text)


@pytest.mark.parametrize("prefix", ["{:>6}\t", "{:>6}→"])
def test_names_in_a_table_read_with_line_numbers(prefix):
    rows = ["name,phone", "Nadia Brennan,(303) 555-0138", f"{ZH_NAME_A},19900003960"]
    text = "\n".join(prefix.format(i + 1) + row for i, row in enumerate(rows))
    assert {"Nadia Brennan", ZH_NAME_A} <= _persons(text)


def test_tsv_markdown_and_chinese_headers():
    tsv = "Name\tTeam\nPriya Lindqvist\tOps\n"
    md = "| Contact Name | Role |\n|---|---|\n| Owen Fairbanks | CFO |\n"
    zh = f"{ZH_HEADER},dept\n{ZH_NAME_B},Ops\n"
    assert "Priya Lindqvist" in _persons(tsv) and "Owen Fairbanks" in _persons(md) and ZH_NAME_B in _persons(zh)


def test_a_first_name_column_takes_single_words():
    text = "first_name,last_name,city\nMaya,Nakamura,Riverton\n"
    assert {"Maya", "Nakamura"} <= _persons(text)


@pytest.mark.parametrize("text", [
    "name,version\nrequests,2.31.0\nDjango,4.2\nnumpy,1.26\n",          # package lists
    "product name,price\nSteel Pump,40\nBlue Valve,12\n",               # not a person column
    "file_name,size\nReport Final.pdf,12\n",
    "name,email\nN/A,x@example.com\n12345,y@example.com\n",
])
def test_tables_without_person_names_are_left_alone(text):
    assert not _persons(text)


# --- Chinese name lists after a label, and names that share a column with a known person

ZH_NAME_C = "顾诗琪"
ZH_ATTENDEES = "参会人"      # attendees
ZH_RECORDER = "记录人"       # minutes taken by
ZH_BY, ZH_OWNS = "由", "负责"   # "by ... is responsible"


def test_every_name_in_a_chinese_list_after_a_label_is_found():
    text = f"{ZH_ATTENDEES}：{ZH_NAME_C}、{ZH_NAME_A}、{ZH_NAME_B}\n{ZH_RECORDER}：{ZH_NAME_B}\n"
    assert {ZH_NAME_C, ZH_NAME_A, ZH_NAME_B} <= _persons(text)


def test_a_chinese_name_between_by_and_responsible_is_found():
    text = f"本事项{ZH_BY}{ZH_NAME_A}{ZH_OWNS}，下周五前提交方案。"
    assert ZH_NAME_A in _persons(text)


def test_cells_in_the_column_of_a_known_person_are_persons():
    from hermie.gate.types import Finding
    text = ("<PERSON_6> | Operations | 181200\n"
            f"{ZH_NAME_A} | Finance | 168400\n"
            "Daniel Fairbanks | Legal | 161200\n"
            "Steel | Ops | 12\n")
    seed = [Finding("PERSON", 0, len("<PERSON_6>"), 1.0)]
    got = {text[f.start:f.end] for f in rules.column_persons(text, seed)}
    assert got == {ZH_NAME_A, "Daniel Fairbanks"}


def test_no_column_spread_without_a_person_in_the_column():
    from hermie.gate.types import Finding
    text = "Daniel Fairbanks | Legal\nOwen Fairbanks | Ops\n"
    assert rules.column_persons(text, [Finding("EMAIL_ADDRESS", 0, 3, 1.0)]) == []
    assert rules.column_persons("just one line | with a bar", [Finding("PERSON", 0, 4, 1.0)]) == []


def test_names_leading_space_separated_rows_like_a_known_person():
    from hermie.gate.types import Finding
    text = ("<PERSON_6> Operations 181200\n"
            f"{ZH_NAME_A} Finance 168400\n"
            "Daniel Fairbanks Legal 161200\n"
            "total rows 3\n")
    seed = [Finding("PERSON", 0, len("<PERSON_6>"), 1.0)]
    got = {text[f.start:f.end] for f in rules.column_persons(text, seed)}
    assert got == {ZH_NAME_A, "Daniel Fairbanks"}


def test_no_space_column_spread_when_the_person_is_not_at_the_line_start():
    from hermie.gate.types import Finding
    text = "owner is Maya Nakamura\nDaniel Fairbanks Legal 161200\n"
    i = text.index("Maya")
    assert rules.column_persons(text, [Finding("PERSON", i, i + len("Maya Nakamura"), 1.0)]) == []


@pytest.mark.parametrize("line", ["FN:{}", "FN;CHARSET=UTF-8:{}", "N:{}"])
def test_names_in_vcard_name_fields_are_found(line):
    for name in (ZH_NAME_A, "Julian Albright"):
        text = "BEGIN:VCARD\nVERSION:3.0\n" + line.format(name) + "\nTEL;TYPE=CELL:19900006818\nEND:VCARD\n"
        assert name in _persons(text)


@pytest.mark.parametrize("lead", ["甲方将位于", "公司坐落于", "请寄到"])
def test_a_chinese_address_starts_after_the_verb_that_introduces_it(lead):
    addr = "杭州市西湖区星河路376号18栋2903室"   # Hangzhou ... room 2903
    text = lead + addr + "的住房出租给乙方。"
    spans = [text[f.start:f.end] for f in rules.find(text) if f.entity == "ADDRESS"]
    assert spans == [addr]


# --- organizations, money, dates, projects (claimed from 2026-10-19)

def _spans(text, entity):
    return {text[f.start:f.end] for f in rules.find(text) if f.entity == entity}


ZH_CO = "北京市青禾科技有限公司"   # Beijing Qinghe Technology Co., Ltd.
ZH_WITH, ZH_SIGN = "与", "签订合同"                        # "with", "signed the contract"
ZH_OUR_CO = "本公司"                                               # "our company"


@pytest.mark.parametrize("text,org", [
    ("Seller: Silverline Holdings, represented by its director.", "Silverline Holdings"),
    ("We ship via Bluegate Logistics every week.", "Bluegate Logistics"),
    ("Invoice from Acme Widgets Inc. is attached.", "Acme Widgets Inc."),
    ("Harborview Supply Co. will deliver.", "Harborview Supply Co."),
    ("signed with Northwind Traders LLC today", "Northwind Traders LLC"),
    (f"甲方：{ZH_CO}", ZH_CO),
    (f"{ZH_WITH}{ZH_CO}{ZH_SIGN}", ZH_CO),
])
def test_organizations_are_found(text, org):
    assert org in _spans(text, "ORGANIZATION")


@pytest.mark.parametrize("text", [
    "class UserService: pass", "import DataSystems from './x'", "the team decided", f"{ZH_OUR_CO}决定",
    "Please contact the Support Team.",
])
def test_ordinary_words_are_not_organizations(text):
    assert not _spans(text, "ORGANIZATION")


@pytest.mark.parametrize("text,money", [
    ("The penalty is $6,900.00 per week.", "$6,900.00"),
    ("Budget: USD 2,096,500 for the year.", "USD 2,096,500"),
    ("a fee of 1,200 EUR", "1,200 EUR"),
    ("about £7.5m in total", "£7.5m"),
    ("月租金人民币5000元", "人民币5000元"),
    ("押金¥15,600。", "¥15,600"),
    ("预算3.5万元", "3.5万元"),
    ("人民币伍仟元整", "人民币伍仟元整"),
])
def test_money_is_found(text, money):
    assert money in _spans(text, "MONEY")


@pytest.mark.parametrize("text", ["echo $1 $2", "s/(a)/$10/", "price = 0", "costs nothing", "sed 's/x/$100/'"])
def test_shell_parameters_and_code_are_not_money(text):
    assert not _spans(text, "MONEY")


@pytest.mark.parametrize("text,date", [
    ("This agreement is made on August 16, 2026 between", "August 16, 2026"),
    ("signed 16 August 2026 in London", "16 August 2026"),
    ("start date 2023-09-08, reviewed", "2023-09-08"),
    ("due 08/16/2026", "08/16/2026"),
    ("时间：2026年8月25日", "2026年8月25日"),
])
def test_dates_are_found(text, date):
    assert date in _spans(text, "DATE")


@pytest.mark.parametrize("text", [
    "2026-10-19T08:12:03Z ERROR db timeout", "2026-10-19 08:12:03,551 INFO started", "version 1.2.3",
    "commit abc123 Date: Mon Oct 19 08:12:03 2026 +0800",
])
def test_timestamps_in_logs_are_not_dates(text):
    assert not _spans(text, "DATE")


@pytest.mark.parametrize("text,project", [
    ("Subject: Project Bluefin, next steps", "Project Bluefin"),
    ("## 一、远山项目进展", "远山项目"),
    ("代号“长河”", "长河"),
])
def test_project_names_are_found(text, project):
    assert project in _spans(text, "PROJECT")


@pytest.mark.parametrize("text", ["这个项目很大", "开源项目", "the project is late"])
def test_generic_project_words_are_left_alone(text):
    assert not _spans(text, "PROJECT")


def test_table_columns_give_organizations_money_and_dates():
    text = ("name,company,salary,start_date\n"
            "Isabel Brennan,Northwind,53200,2023-09-08\n"
            "Owen Fairbanks,Redwood,161200,2024-01-15\n")
    assert {"Northwind", "Redwood"} <= _spans(text, "ORGANIZATION")
    assert {"53200", "161200"} <= _spans(text, "MONEY")
    assert {"2023-09-08", "2024-01-15"} <= _spans(text, "DATE")


@pytest.mark.parametrize("text,org", [("Client: Acme Corp", "Acme Corp"),
                                      ("Tenant: Northwind Holdings LLC", "Northwind Holdings LLC")])
def test_a_company_after_a_party_label_is_an_organization_not_a_person(text, org):
    assert {(f.entity, text[f.start:f.end]) for f in rules.find(text)} == {("ORGANIZATION", org)}


@pytest.mark.parametrize("text", [
    "// Copyright 2015 Google Inc. All rights reserved.",
    " * Licensed to Elasticsearch B.V. under one or more contributor license agreements.",
    "resource \"aws_autoscaling_group\" uses an Instance Group here",
    "A9 Vulnerable Components",
    "Copyright The Chromium Project Authors.",
    "createOperation Object Project Card",
    "mov ax, $0000",
    "启动项目后运行",                 # "after starting the project, run"
    "这是 一个闭源项目",            # "this is a closed-source project"
])
def test_license_headers_and_code_words_are_not_orgs_projects_or_money(text):
    found = {(f.entity, text[f.start:f.end]) for f in rules.find(text)}
    assert not {e for e, _ in found} & {"ORGANIZATION", "PROJECT", "MONEY"}, found


def test_a_quoted_chinese_project_name_is_found():
    text = "启动“长河”项目"   # start the "Changhe" project
    assert "长河" in _spans(text, "PROJECT")


def test_code_values_in_company_and_project_columns_are_left_alone():
    text = ("company,project,amount\n"
            "government_entity,[https://github.com/x/y],1\n"
            "requirements,user_session,2\n")
    assert not _spans(text, "ORGANIZATION") and not _spans(text, "PROJECT")
