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
                 f"AWS keys: AKIA{r(16, UP)} and {r(40, B64)}", "密码是" + r(12) + "4", "数据库密码：" + r(14) + "8"):
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
for name, addr in [("张伟", "北京市朝阳区建国路88号院3号楼1201室"), ("李秀英", "上海市浦东新区世纪大道100号"), ("王建国", "广东省深圳市南山区科技园南区高新南一道5栋302"),
                   ("欧阳明轩", "四川省成都市武侯区人民南路四段27号"), ("刘芳", "浙江省杭州市西湖区文三路398号")]:
    i, m = _cn_id(), _cn_mobile()
    PII.append((f"房屋租赁合同\n甲方（出租人）：{name}\n身份证号：{i}\n住址：{addr}\n联系电话：{m}\n乙方（承租人）：见附件",
                [(name, "PERSON"), (i, "ID"), (addr, "ADDRESS"), (m, "PHONE")]))
PII += [
    ("本人周杰，身份证号码" + (zi := _cn_id()) + "，现住湖北省武汉市洪山区珞喻路1037号，自愿签署本协议。", [("周杰", "PERSON"), (zi, "ID"), ("湖北省武汉市洪山区珞喻路1037号", "ADDRESS")]),
    ("收货地址：北京市海淀区中关村大街1号海龙大厦2层", [("北京市海淀区中关村大街1号海龙大厦2层", "ADDRESS")]),
    ("员工李明（工号E10234）家庭住址为上海市徐汇区漕溪北路88号", [("李明", "PERSON"), ("上海市徐汇区漕溪北路88号", "ADDRESS")]),
    ("请联系陈先生，电话" + (cm := _cn_mobile()), [("陈", "PERSON"), (cm, "PHONE")]),
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
    ("乙方：赵丽颖，居民身份证号码：" + (zid := _cn_id()), [("赵丽颖", "PERSON"), (zid, "ID")]),
    ("家庭住址：广东省广州市天河区体育西路103号维多利广场B座2305室", [("广东省广州市天河区体育西路103号维多利广场B座2305室", "ADDRESS")]),
    ("联系人：孙小红 电话：13912345678", [("孙小红", "PERSON"), ("13912345678", "PHONE")]),
    ("收件人：吴天宇 地址：重庆市渝中区解放碑民权路28号", [("吴天宇", "PERSON"), ("重庆市渝中区解放碑民权路28号", "ADDRESS")]),
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
    assert "CN_ID_CARD" in {f.entity for f in rules.find("身份证号：" + _cn_id())}
    assert "CN_MOBILE" in {f.entity for f in rules.find("手机" + _cn_mobile())}


def test_a_chinese_id_number_needs_a_valid_checksum():
    bad = _cn_id()[:-1] + ("0" if _cn_id()[-1] != "0" else "1")
    ok = _cn_id()
    assert not [f for f in rules.find("编号 " + bad + " 结束") if f.entity == "CN_ID_CARD"] or bad == ok
    assert [f for f in rules.find("编号 " + ok + " 结束") if f.entity == "CN_ID_CARD"]


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
    "甲方：北京某某科技有限公司",
    "Customer Service Representative: please escalate",
    "Name: str = Field(...)",
    "Contact: support@example.com",
    "同事王芳明天请假，不用等她",
    "住址：见附件",
    "地址：待定",
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
