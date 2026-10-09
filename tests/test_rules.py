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
    "Client: Acme Corp",
    "Tenant: Northwind Holdings LLC",
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
