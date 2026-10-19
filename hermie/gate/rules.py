"""Pure-regex detectors that need no NLP model: credentials (provider formats, names that say "secret", headers, URLs,
high-entropy tokens) and the personal data of contracts and forms (names after a party label, ID numbers, street
addresses, in English and Chinese). `find` is the whole interface; the Presidio layer in `recognizers.scan` adds
these findings to its own.

Precision matters as much as recall here: a placeholder in the wrong place confuses the model, so every detector
either needs a distinctive shape, a label that says what follows, or a checksum."""
from __future__ import annotations

import base64
import binascii
import math
import bisect
import re

from .types import Finding

# ---------------------------------------------------------------------------------------------------- credentials

# (name, regex, score). Patterns with a capture group (VALUE_ONLY) report only group 1, so `.env` key names and URL
# schemes stay readable.
SECRET_PATTERNS = [
    ("openai_style_key", r"(?<![A-Za-z0-9])sk-(?:[A-Za-z0-9]+-)?[A-Za-z0-9_-]{16,}", 0.85),
    ("anthropic_key", r"(?<![A-Za-z0-9])sk-ant-[A-Za-z0-9_-]{20,}", 0.9),
    # access key ids and the other IAM unique-id prefixes (user, role, SSH key, group, ...)
    ("aws_access_key", r"(?<![A-Za-z0-9])(?:AKIA|ASIA|AIDA|AROA|AIPA|ANPA|ANVA|APKA|AGPA)[0-9A-Z]{16,17}(?![A-Za-z0-9])", 0.85),
    ("nkey_seed", r"(?<![A-Z2-7])S[UANOCPX][A-Z2-7]{56}(?![A-Z2-7])", 0.9),
    ("github_token", r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{30,}", 0.85),
    ("github_fine_grained", r"(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{22,}", 0.9),
    ("gitlab_token", r"(?<![A-Za-z0-9])gl(?:pat|dt|rt|ptt|cbt|oas|soat)-[A-Za-z0-9_-]{20,}", 0.9),
    ("slack_token", r"(?<![A-Za-z0-9])xox[abprs]-[A-Za-z0-9-]{10,}", 0.85),
    ("slack_webhook", r"https://hooks\.slack\.com/services/([A-Za-z0-9/]{20,})", 0.9),
    ("discord_webhook", r"https://discord(?:app)?\.com/api/webhooks/([0-9]{6,}/[A-Za-z0-9_-]{20,})", 0.9),
    ("google_api_key", r"(?<![A-Za-z0-9])AIza[0-9A-Za-z_-]{35}(?![A-Za-z0-9])", 0.85),
    ("google_oauth", r"(?<![A-Za-z0-9])ya29\.[A-Za-z0-9_-]{20,}", 0.85),
    ("stripe_key", r"(?<![A-Za-z0-9])[sr]k_(?:live|test)_[A-Za-z0-9]{16,}", 0.85),
    ("stripe_webhook", r"(?<![A-Za-z0-9])whsec_[A-Za-z0-9]{24,}", 0.9),
    ("twilio", r"(?<![A-Za-z0-9])SK[0-9a-fA-F]{32}(?![A-Za-z0-9])", 0.7),
    ("sendgrid", r"(?<![A-Za-z0-9])SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}", 0.9),
    ("mailgun", r"(?<![A-Za-z0-9])key-[0-9a-f]{32}(?![A-Za-z0-9])", 0.85),
    ("hf_token", r"(?<![A-Za-z0-9])hf_[A-Za-z0-9]{30,}", 0.85),
    ("npm_token", r"(?<![A-Za-z0-9])npm_[A-Za-z0-9]{30,}", 0.85),
    ("pypi_token", r"(?<![A-Za-z0-9])pypi-[A-Za-z0-9_-]{30,}", 0.85),
    ("docker_pat", r"(?<![A-Za-z0-9])dckr_pat_[A-Za-z0-9_-]{20,}", 0.9),
    ("shopify", r"(?<![A-Za-z0-9])shp(?:at|ca|pa|ss)_[a-f0-9]{32}", 0.9),
    ("digitalocean", r"(?<![A-Za-z0-9])do[opr]_v1_[a-f0-9]{64}", 0.9),
    ("databricks", r"(?<![A-Za-z0-9])dapi[a-f0-9]{32}(?![A-Za-z0-9])", 0.85),
    ("postman", r"(?<![A-Za-z0-9])PMAK-[A-Za-z0-9-]{40,}", 0.9),
    ("linear", r"(?<![A-Za-z0-9])lin_api_[A-Za-z0-9]{40}", 0.9),
    ("notion", r"(?<![A-Za-z0-9])(?:secret|ntn)_[A-Za-z0-9]{36,}", 0.8),
    ("telegram_bot", r"(?<![A-Za-z0-9])[0-9]{8,10}:[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])", 0.85),
    ("sentry_dsn", r"https://([0-9a-f]{32})@[A-Za-z0-9.-]*sentry\.io", 0.9),
    ("azure_storage_key", r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{86}==(?![A-Za-z0-9+/=])", 0.85),
    # a whole block; without an END line, the header and the base64 / armor-header lines right after it (a header named
    # in prose does not take the rest of the text)
    ("private_key_block", r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----(?:[\s\S]*?-----END [A-Z ]*PRIVATE KEY(?: BLOCK)?-----"
                          r"|(?:(?:\\n|\r?\n|[ \t\"'+,])+(?:[A-Za-z0-9+/=]{4,}|[A-Za-z-]+:[^\n\\]*))*)", 0.95),
    ("jwt", r"(?<![A-Za-z0-9])eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}", 0.8),
    # user:password@ in any URL (the user may be empty: redis://:password@host)
    ("db_url_credentials", r"(?i)(?<![a-z0-9+.-])[a-z][a-z0-9+.-]*://[^\s:/@]*(?:@[^\s:/@]+)?:([^\s@/]{4,})@", 0.9),
    # Authorization-style headers and the schemes themselves
    ("auth_header", r"(?i)(?:authorization|proxy-authorization|x-api-key|api-key|x-auth-token|x-access-token|x-token)[\"']?\s*[:=]\s*[\"']?"
                    r"(?:(?:bearer|basic|token|apikey)\s+)?([A-Za-z0-9._~+/=-]{12,})", 0.85),
    ("bearer_value", r"(?i)\b(?:bearer|basic|ntlm|negotiate)\s+([A-Za-z0-9._~+/=-]{16,})", 0.8),
    ("oauth_param", r"(?i)\boauth_(?:consumer_key|token(?:_secret)?|signature|verifier)(?![a-z_])[\\\"']*\s*[:=]\s*[\\\"']*"
                    r"([A-Za-z0-9%._~+/=-]{6,})", 0.8),
    ("curl_option", r"CURLOPT_(?:PROXY)?(?:PASSWORD|USERPWD|KEYPASSWD|XOAUTH2_BEARER|TLSAUTH_PASSWORD)\s*,\s*\"([^\"]{3,})\"", 0.85),
    # SQL: IDENTIFIED BY '...', PASSWORD '...', and CREATE USER / ROLE ... PASSWORD bare_value
    ("sql_password", r"(?i)\b(?:IDENTIFIED\s+BY|PASSWORD)\s+['\"]([^'\"\s]{4,64})['\"]", 0.8),
    ("sql_password_bare", r"(?i)\bCREATE\s+(?:USER|ROLE)\b[^;\n]{0,80}?\bPASSWORD\s+(?!['\"])([A-Za-z0-9_!@#%^&*.-]{4,64})", 0.75),
    # command lines: --password x, -Password x, mysql -px, ConvertTo-SecureString 'x', openssl hexkey:...
    ("cli_password", r"(?i)(?<![\w-])--?(?:password|passwd|pass)(?:\s+|=)['\"]?(?:pass:)?(?![-(\[])([^\s'\"$<>{}]{3,128})", 0.75),
    ("mysql_password", r"\bmysql(?:dump|admin)?\b[^\n]{0,80}?(?<!\S)-p([^\s'\"-][^\s'\"]{2,63})", 0.8),
    ("secure_string", r"(?i)ConvertTo-SecureString\s+(?:-String\s+)?['\"]([^'\"]{4,128})['\"]", 0.85),
    ("openssl_hex_key", r"(?i)\bhex(?:key|secret|pass|seed|ikm|salt)\s*:\s*([0-9A-Fa-f]{8,})", 0.8),
    ("curl_user", r"(?<![A-Za-z0-9])(?:-u|--user)\s+[^\s:]+:([^\s\"']{4,})", 0.8),
    ("url_query_secret", r"(?i)[?&](?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|token|secret|client[_-]?secret|sig|signature|key)="
                         r"([A-Za-z0-9._~+/%=-]{8,})", 0.8),
    ("url_query_password", r"(?i)[?&](?:password|passwd|pwd|pass)=([A-Za-z0-9._~+/%=!*-]{4,})", 0.8),
    # setters and constructors given a literal: setPassword("..."), setClientSecret("..."), new Credential("...")
    ("setter_literal", r"(?:\bset|\bnew\s+)(?:[A-Z]\w*)?(?:Password|Passwd|Secret|Token|ApiKey|Credential)\(\s*[\"']([^\"'\s]{4,128})[\"']\s*\)", 0.8),
    # "the password is hunter2xyz", "my api key = ...", and the Chinese words for password / secret key / token
    ("secret_phrase", r"(?i)\b(?:api[ _-]?key|secret(?: key)?|access token|token|password|passcode|passphrase|credentials?)\s+(?:is|was|=|:)\s+[\"']?"
                      r"([^\s\"',;()\[\]{}<>]{8,})", 0.7),
    ("secret_phrase_zh", r"(?:\u5bc6\u7801|\u53e3\u4ee4|\u5bc6\u94a5|\u79d8\u94a5|\u4ee4\u724c)(?:\u662f|\u4e3a|\uff1a|:|=)\s*[\"']?"
                         r"([A-Za-z0-9_\-/+=.@#$%!&*^~?]{6,})", 0.7),
    # Assignment form: api_key=..., password: ..., DEEPSEEK_API_KEY="..." (the value is group 1; the Chinese words for
    # password and secret key are \u escapes; the separator class also accepts the full-width colon).
    ("credential_assignment",
     r"(?i)(?:api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|private[_-]?key|"
     r"app[_-]?secret|password|passwd|secret|token|\u5bc6\u7801|\u5bc6\u94a5)\s*[:=\uff1a]\s*[\"']?"
     r"([A-Za-z0-9_\-/+=.@#$%!&*^~?]{8,})", 0.7),
]
VALUE_ONLY = {"db_url_credentials", "credential_assignment", "slack_webhook", "discord_webhook", "sentry_dsn", "auth_header",
              "bearer_value", "curl_user", "url_query_secret", "secret_phrase", "secret_phrase_zh", "oauth_param", "curl_option",
              "sql_password", "sql_password_bare", "cli_password", "mysql_password", "secure_string", "openssl_hex_key",
              "url_query_password", "setter_literal"}
_TYPED_VALUES = {"credential_assignment", "secret_phrase", "auth_header", "bearer_value", "url_query_secret", "curl_user",
                 "secret_phrase_zh", "db_url_credentials", "oauth_param", "curl_option", "sql_password", "sql_password_bare",
                 "cli_password", "mysql_password", "secure_string", "url_query_password",
                 "setter_literal"}   # a placeholder or example word there is not a secret
_PASSWORD_SHAPES = {"db_url_credentials", "curl_user", "curl_option", "sql_password", "sql_password_bare", "cli_password",
                   "mysql_password", "secure_string", "url_query_password", "setter_literal"}
_DUMMY_CHECKED = {"db_url_credentials", "curl_user", "curl_option", "sql_password", "sql_password_bare", "cli_password",
                  "mysql_password", "secure_string", "oauth_param", "url_query_secret", "credential_assignment",
                  "secret_phrase", "url_query_password", "setter_literal"}

_COMPILED_SECRETS = [(n, re.compile(rx), s, n in VALUE_ONLY) for n, rx, s in SECRET_PATTERNS]

_PLACEHOLDER = re.compile(r"<[A-Z_]+_\d+>")
_IDENT_CHAIN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")
_SNAKE_OR_KEBAB = re.compile(r"[a-z0-9]+(?:[_-][a-z0-9]+)+")
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_CODE_WORDS = {"optional", "none", "null", "true", "false", "string", "str", "int", "bool", "bytes", "any", "self",
               "undefined", "required", "default", "environ", "changeme", "example", "placeholder", "skip", "ignore", "ignored",
               "varchar", "text", "char", "integer", "boolean", "number", "float", "double", "date", "datetime", "timestamp",
               "blob", "binary"}
# What people write in an example URL or curl command instead of a password (`user:pass@host`, `admin:admin`).
_DUMMY = {"password", "passwd", "pass", "pwd", "secret", "admin", "root", "user", "username", "test", "guest", "demo",
          "changeme", "example", "foobar", "foo", "bar", "123456", "1234567", "12345678", "123456789", "abcdefgh", "qwerty",
          # the word "password" in other languages (UI text under a password key)
          "passwort", "senha", "wachtwoord", "parola", "haslo", "salasana", "adgangskode", "motdepasse", "contrasena", "clave"}
_UI_TEXT_NAMES = {"label", "forgot", "title", "placeholder", "hint", "tooltip", "msg", "message", "text", "strength", "error"}


def _low_variety(val: str, password: bool = False) -> bool:
    """00000000, xxxxxx, 0123456789, abcdefgh, ef012345ef012345, 000102030405...: too regular to be generated. Under a
    password name only the first three count: people do pick aaaabbbb1 or abc123abc123."""
    low = val.lower()
    if len(set(low)) <= (2 if password or len(low) < 8 else 3):
        return True
    if len(low) >= 6 and (low in "0123456789abcdefghijklmnopqrstuvwxyz" or low in "9876543210"):
        return True
    if password:
        return False
    if len(low) >= 12 and any(low == (low[:k] * (len(low) // k + 1))[:len(low)] for k in range(2, min(len(low) // 2, 64) + 1)):
        return True                        # a unit repeated (ef012345ef012345, a vector written twice)
    if len(low) >= 16 and re.fullmatch(r"(?:[0-9a-f]{2})+", low):
        b = [int(low[i:i + 2], 16) for i in range(0, len(low), 2)]
        if len({(y - x) % 256 for x, y in zip(b, b[1:])}) == 1:
            return True                    # counting bytes: 000102..., 00112233...
    return False


def _self_described_dummy(val: str) -> bool:
    """A stand-in, not a password: `password`, `admin`, `xxx`. Weak real passwords (`admin2024`, `root123`) and
    test-looking values (`demo-password-not-real`) are still hidden: a placeholder there costs nothing."""
    low = val.lower()
    return low in _DUMMY or len(set(low)) == 1


_HEX_CONSTANT = re.compile(r"0[xX][0-9a-fA-F]{1,10}")


def _entropy(s: str) -> float:
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


# References to a value, not the value: ${X}, $(cmd), {{ x }}, %VAR%, %s, $ENV_NAME, $php_var, <your password>, @ivar, C:\
_REFERENCE = re.compile(r"\$\{|\$\(|\{\{|%\w+%|%\(?\w*\)?[sd](?!\w)|\$[A-Z_][A-Z0-9_]*(?!\w)|\$[a-z_][a-z0-9_]*(?!\w)"
                        r"|<[a-z_][a-z_ ]*>?$|@[a-z_][a-z0-9_]*$|[A-Za-z]:\\+$")


def _is_placeholder_value(v: str) -> bool:
    return bool(_PLACEHOLDER.search(v)) or v.startswith("{") or v.lower() in _CODE_WORDS or bool(_REFERENCE.match(v))


# Variable names: any name with one of these as a segment (ALL_CAPS, snake_case, kebab-case, camelCase) holds a secret.
_SENSITIVE = {"key", "keys", "secret", "secrets", "token", "tokens", "password", "passwd", "pwd", "pass", "passphrase", "credential",
              "credentials", "creds", "auth", "authorization", "bearer", "signature", "salt", "dsn", "apikey", "privatekey", "secretkey",
              "accesskey", "authtoken"}
_PASSWORDISH = {"password", "passwd", "pwd", "pass", "passphrase"}
_NOT_A_SECRET_TAIL = {"path", "file", "dir", "directory", "name", "length", "size", "count", "type", "len", "ttl", "timeout", "expiry",
                      "expires", "header", "headers", "url", "uri", "endpoint", "host", "domain", "field", "column", "id_field", "prefix",
                      "format", "algorithm", "alg", "mode", "version", "label", "title", "description", "env", "var", "variable",
                      "error", "errors", "status", "checksum", "hash", "pattern", "regex", "msg", "message", "hint", "tooltip",
                      "method", "provider", "scheme", "strategy", "backend", "mechanism", "parameter", "param", "params",
                      "string"}
# A name with one of these segments holds something else: a public key, a password hash
_NOT_A_SECRET_ANY = {"public", "hash", "hashed", "digest", "encrypted", "cache", "sort", "primary", "foreign", "partition",
                     "topology", "idempotency", "lookup", "routing", "shard", "protobuf"}
# Names that say the value is a secret (not just `key`, which also names dict keys, primary keys, sort keys).
_STRONG = {"secret", "secrets", "token", "tokens", "credential", "credentials", "creds", "apikey", "secretkey",
           "accesskey", "authtoken", "privatekey", "passphrase", "salt"}
# `key` after one of these is strong too: api_key, ACCESS_KEY, client-key, signing_key
_KEY_KINDS = {"api", "access", "secret", "private", "app", "client", "master", "signing", "encryption", "consumer", "license",
              "service", "account"}
_SEGMENT = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
_ASSIGN = re.compile(r"""(?P<name>[A-Za-z_][A-Za-z0-9_.\-]{0,63})["']?[ \t]*(?::=|=>|=|:)[ \t]*["']?(?P<val>[^\s"'`,;<>(){}\[\]\\]{8,})""")
# _ASSIGN stops at the first bracket or semicolon; password managers use those. These two take the whole value: inside
# quotes, or the rest of the line in an env file (`DB_PASSWORD=k7#Lq;9vX)2m`) or a YAML mapping.
_ASSIGN_QUOTED = re.compile(r"""(?P<name>[A-Za-z_][A-Za-z0-9_.\-]{0,63})["']?[ \t]*(?::=|=>|=|:)[ \t]*"""
                            r"""(?:(?:\[\]byte|str|(?:[A-Za-z_][\w.]*?)?(?:Some|String|Str|Secret|Value|Password|bytes|unhexlify|fromhex|Uint8Array|"""
                            r"""HexToBytes|hexToBytes|b64decode|Base64Decode|base64Decode|FromBase64String))\()?"""
                            r"""(?:"(?P<dq>[^"\s]{4,128})"|'(?P<sq>[^'\s]{4,128})')""")
_ASSIGN_LINE = re.compile(r"""(?m)^[ \t]*(?:export[ \t]+)?(?:(?P<env>[A-Z][A-Z0-9_]{0,63})=|(?P<yaml>[A-Za-z_][A-Za-z0-9_.\-]{0,63}):[ \t]+"""
                          r"""|(?P<ini>[A-Za-z_][A-Za-z0-9_.\-]{0,63})[ \t]*=[ \t]*)"""
                          r"""(?P<line>[^\s"'][^\s]{3,127})[ \t]*$""")
# NAME=value inside a command or a YAML list item (`heroku config:set SMTP_PASSWORD=...`, `- JWT_SECRET=...`, `... \`)
_ENV_INLINE = re.compile(r"""(?<![\w.$-])(?P<name>[A-Z][A-Z0-9]*_[A-Z0-9_]{1,62})=(?P<val>[^\s"'`\\$]{3,128})(?=[ \t]|$)""", re.M)
# A call, an expansion, a template or a list is code, not a value.
_CODE_VALUE = re.compile(r"^[A-Za-z_][\w.]*\(.*\)$|\$\{|\$\(|\{\{|^\[")


def _passwordish(segs: list[str]) -> bool:
    """`pass` names a password only at the end (DB_PASS, admin_pass), not in pass_through or pass_count."""
    return (bool(set(segs) & (_PASSWORDISH - {"pass"})) or (bool(segs) and segs[-1] == "pass")
            or any(a == "pass" and b == "phrase" for a, b in zip(segs, segs[1:])))


def _segments(name: str) -> list[str]:
    return [s.lower() for s in _SEGMENT.findall(name)]


_WORDISH = re.compile(r"[A-Z][a-z]{2,}|[a-z]{4,}|[A-Z]{6,}(?![a-z])")


def _wordish_fraction(val: str) -> float:
    return sum(len(m.group()) for m in _WORDISH.finditer(val)) / max(len(val), 1)


def _secretish(val: str, wordy_ok: bool = False, password: bool = False) -> bool:
    """Generated secrets nearly always contain a digit; a long mixed-case high-entropy string without one counts too.
    Plain words, snake_case names and CamelCase identifiers do not, unless `wordy_ok` (under a password name people
    pick words plus digits: `Password123`)."""
    if re.fullmatch(r"[+-]?[\d.]+(?:[eE][+-]?\d+)?", val) and "." in val:
        return False                       # a float
    if _HEX_CONSTANT.fullmatch(val) or _low_variety(val, password):
        return False                       # a numeric constant (STATUS_NO_TOKEN = 0xC000007C), 000000, 0123456789
    if (_wordish_fraction(val) >= 0.6 and not wordy_ok) or (_SNAKE_OR_KEBAB.fullmatch(val) and not _UUID.fullmatch(val)):
        return False                       # CamelCase / snake_case identifiers, model ids, file names
    if any(c.isdigit() for c in val):
        return True
    if wordy_ok and any(c.isalpha() for c in val) and any(not c.isalnum() and c not in "_-." for c in val):
        return True                        # letters and symbols: a password manager's output
    return len(val) >= 32 and any(c.islower() for c in val) and any(c.isupper() for c in val) and _entropy(val) >= 4.2


_SNAKE_WORDS = re.compile(r"[a-z]+(?:[_-][a-z]+)+")
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+")    # SECRET_KEY: the name of a variable, not its value
# ClientSecret, myAuthID, Force: words, each hump at least three letters (a generated value has shorter humps)
_CAMEL_WORDS = re.compile(r"[a-z]{2,}(?:[A-Z][a-z]{2,})+(?:[A-Z]{2,4})?|[A-Z][a-z]{2,}(?:[A-Z][a-z]{2,})*(?:[A-Z]{2,4})?")
_ID_PART = re.compile(r"[a-z][a-z0-9]*|[A-Z][A-Z0-9]*|[A-Z][a-z][a-z0-9]*|\d+")


def _identifier_like(val: str) -> bool:
    """Names joined by `_`, `-` or `:` (ALICE_cf_prime239v2_PUB, KAS-ECC-CDH_K-571_C2): each part one case, at least half
    of them words. A generated token mixes cases inside its parts; a UUID is not a name."""
    parts = re.split(r"[_:-]", val)
    if len(parts) < 2 or _UUID.fullmatch(val) or not all(p and len(p) <= 16 and _ID_PART.fullmatch(p) for p in parts):
        return False
    return 2 * sum(p.isalpha() and len(p) >= 2 for p in parts) >= len(parts)


def _value_looks_secret(val: str, segs: list[str], literal: bool = False, env_name: bool = False) -> bool:
    """`literal`: the value is quoted or is the rest of an env / YAML line, so it is data, not a variable. `env_name`:
    the name is ALL_CAPS (SESSION_SECRET), where a plain word is configuration, not a lexer token."""
    val = val.rstrip(".")
    passwordish = _passwordish(segs)
    strong = literal and not passwordish and bool(set(segs) & _STRONG or ("key" in segs and set(segs) & _KEY_KINDS))
    if len(val) < (4 if passwordish and literal else 6 if strong else 8) or _is_placeholder_value(val) or _IDENT_CHAIN.fullmatch(val):
        return False
    if _HEX_CONSTANT.fullmatch(val) or _low_variety(val, passwordish) or _self_described_dummy(val):
        return False
    if not literal and (_CAMEL_WORDS.fullmatch(val) or val.startswith(("$", "@", "%", "<"))):
        return False                       # `pass: PassEvent,`, `password => $password`: names in code
    if val.startswith(("/", "./", "../", "~/", "file:", "http://", "https://")) and "@" not in val and val.count("/") >= 2:
        return False
    if _CODE_VALUE.search(val):
        return False
    if strong:
        # a literal under `secret` / `token` / `api_key`: digits or symbols from 6 characters, letters only from 12
        # (`{"token": "identifier"}` in a lexer is a word); camelCase of one or two words is a name (ClientSecret)
        if not val.isascii() or set(segs) & _UI_TEXT_NAMES:
            return False
        camel_name = bool(_CAMEL_WORDS.fullmatch(val)) and len(re.findall(r"[A-Z][a-z]", val)) + val[0].islower() <= 2
        return (len(val) >= ((8 if env_name else 12) if val.isalpha() else 6) and not _ENV_NAME.fullmatch(val) and not _SNAKE_WORDS.fullmatch(val)
                and not camel_name)
    if passwordish and literal:
        # under a password name a literal is the password, plain words and test values included; UI text is not
        return val.isascii() and not set(segs) & _UI_TEXT_NAMES
    if not passwordish and _identifier_like(val):
        return False                       # under a plain `key` name: ALICE_cf_prime239v2_PUB
    return _secretish(val, wordy_ok=passwordish, password=passwordish)


def _assignments(text: str):
    """(name, value, value start, literal) for every assignment shape; a line value ending in `,` or `;` is code."""
    for m in _ASSIGN.finditer(text):
        yield m.group("name"), m.group("val"), m.start("val"), False
    for m in _ASSIGN_QUOTED.finditer(text):
        g = "dq" if m.group("dq") is not None else "sq"
        yield m.group("name"), m.group(g), m.start(g), True
    for m in _ASSIGN_LINE.finditer(text):
        v = m.group("line")
        code = v.endswith((",", ";"))      # `password: password,` in code: judged without the comma, as code
        if code and not m.group("ini"):
            v = v.rstrip(",;")
        if m.group("ini") is not None:
            name = m.group("ini")
            if code or re.search(r"[_.(\[]|[a-z][A-Z]", v):
                continue                   # `password = request.form[...]`: code, which _ASSIGN already sees
            if name == name.lower() and "." not in name and len(v) < 12 and re.fullmatch(r"[a-z_][a-z0-9_]*", v):
                continue                   # `token = tok123`: a variable assigned in code (~/.aws/credentials is not)
            yield m.group("ini"), v, m.start("line"), True
        else:
            yield m.group("env") or m.group("yaml"), v, m.start("line"), not code
    for m in _ENV_INLINE.finditer(text):
        yield m.group("name"), m.group("val"), m.start("val"), True


def _name_driven(text: str) -> list[Finding]:
    out = []
    for name, val, start, literal in _assignments(text):
        segs = _segments(name)
        if not segs or not set(segs) & _SENSITIVE or segs[-1] in _NOT_A_SECRET_TAIL or set(segs) & _NOT_A_SECRET_ANY:
            continue
        if _value_looks_secret(val, segs, literal, name.isupper()):
            out.append(Finding("SECRET", start, start + len(val) - (1 if val.endswith(".") else 0), 0.75))
    return out


_BIG_TOKEN = re.compile(r"(?<![A-Za-z0-9+/_=-])[A-Za-z0-9+/_-]{40,}={0,2}(?![A-Za-z0-9+/_=-])")
# hash prefixes, and the base64 starts of JPEG, PNG, GIF, PDF and ZIP files
_NOT_A_TOKEN_PREFIX = re.compile(r"^(?:sha\d{1,3}|md5)-|^(?:/9j/|iVBORw0KGgo|R0lGOD|JVBERi|UEsDB)")
_AFTER_B64 = re.compile(r"(?:base64,|(?:integrity|node_id)[\"']?\s*[:=]\s*[\"']?|sha\d{0,3}-)$", re.I)
# public keys, certificates, CSRs, CRLs and parameters in PEM armor
_PUBLIC_ARMOR = re.compile(r"-----BEGIN (?:[A-Z0-9 ]*PUBLIC KEY|(?:TRUSTED )?CERTIFICATE(?: REQUEST)?|X509 CRL|[A-Z ]*PARAMETERS)-----"
                           r"[\s\S]*?-----END [A-Z0-9 ]+-----")
# crypt(3) / PHC password hashes ($6$..., $2b$..., $argon2id$...): a hash, not the password
_CRYPT_HASH = re.compile(r"\$(?:argon2(?:id|i|d)|scrypt|pbkdf2[\w-]*|2[abxy]?|[156]|[a-z0-9]{1,4})\$[^\s\"'`]{8,}")
_PUBLIC_NAME = re.compile(r"public[_-]?key[\w-]*[\"']?\s*[:=]\s*[\"']?$", re.I)


# One line of a multi-line base64 body, bare or inside a string literal ("...\n" +).
_B64_BODY_LINE = re.compile(r"""(?m)^[ \t]*(?:["'+]+[ \t]*)?([A-Za-z0-9+/]{40,}={0,2})(?:\\n)?["']?[ \t]*[+,]?[ \t]*\r?$""")
# The first bytes of a DER private key: RSA (PKCS#1), PKCS#8, EC.
_DER_PRIVATE = re.compile(r"M[A-Za-z0-9+/]{2,5}(?:IBAAK|IBADAN|AgEAM|CAQEE)")
# Inside the first line of a DER certificate or public key (version tag, rsaEncryption + BIT STRING, EC point, Ed25519)
_PUBLIC_DER_MARKS = ("gAwIBAgI", "oAMCAQIC", "AQEFAAOC", "DQgAE", "DYgAE", "CQgAE", "K2VwAyEA", "K2VxAzoA")
# Starts of encoded files that hold no credential: PDF, PNG, JPEG, GIF, ZIP / Office, TIFF, BMP, XML, SVG, HTML
_FILE_MAGIC = ("JVBERi", "iVBORw0KGgo", "/9j/", "R0lGOD", "UEsDB", "SUkqA", "TU0AK", "Qk", "PD94bWwg", "PHN2Zy", "PCFET0NUWVBF",
               "PGh0bWw")
_MIME_BASE64 = re.compile(r"(?i)content-transfer-encoding:\s*base64")


def _public_block(text: str, start: int, first: str) -> bool:
    """A base64 block known to hold no secret: a certificate or public key, a known file type, or a mail attachment.
    Base64 of PEM or JSON text (a wrapped key file or service account) and DER private keys are never public."""
    try:
        head = base64.b64decode(first[:len(first) // 4 * 4] + "")[:64]
    except (ValueError, binascii.Error):
        head = b""
    if head.lstrip().startswith((b"-----BEGIN", b"{", b"[")) or b"PRIVATE" in head or _DER_PRIVATE.match(first):
        return False
    if first.startswith(_FILE_MAGIC) or any(mk in first[:48] for mk in _PUBLIC_DER_MARKS):
        return True
    return bool(_MIME_BASE64.search(text, max(0, start - 400), start))


def _encoded_blocks(text: str) -> list[tuple[int, int]]:
    """Spans of 4+ consecutive base64 body lines that `_public_block` recognizes. Their lines are random by nature, so
    entropy says nothing there; any other block (a list of keys, a key body without its header, base64 of a key file)
    keeps its entropy findings."""
    spans, run = [], []
    for m in _B64_BODY_LINE.finditer(text):
        if run and text.count("\n", run[-1][1], m.start()) > 1:
            run = []
        run.append((m.start(), m.end(), m.group(1)))
        if len(run) == 4 and _public_block(text, run[0][0], run[0][2]):
            spans.append([run[0][0], m.end()])
        elif len(run) > 4 and spans and spans[-1][0] == run[0][0]:
            spans[-1][1] = m.end()
    return [(a, b) for a, b in spans]


def _entropy_tokens(text: str) -> list[Finding]:
    """A long mixed-case, digit-bearing, high-entropy token that nothing else explained (an AWS secret key, an Azure
    key, a random session secret). Hashes, integrity strings, inline images and multi-line encoded blocks are
    excluded."""
    out = []
    blocks = _encoded_blocks(text) if text.count("\n") >= 3 else []
    if "$" in text:
        blocks += [m.span() for m in _CRYPT_HASH.finditer(text)]
    if "-----BEGIN" in text:
        blocks += [m.span() for m in _PUBLIC_ARMOR.finditer(text)]
    for m in _BIG_TOKEN.finditer(text):
        if blocks and any(a <= m.start() < b for a, b in blocks):
            continue
        tok = m.group().rstrip("=")
        if _NOT_A_TOKEN_PREFIX.match(tok) or _AFTER_B64.search(text[max(0, m.start() - 24):m.start()]):
            continue
        if _PUBLIC_NAME.search(text[max(0, m.start() - 40):m.start()]):
            continue                       # a public key is meant to be shared
        if not (any(c.islower() for c in tok) and any(c.isupper() for c in tok) and any(c.isdigit() for c in tok)):
            continue
        if (tok.count("/") >= 3 and tok.count("/") * 20 > len(tok)) or _wordish_fraction(tok) >= 0.6:   # a path, words
            continue
        if _entropy(tok) >= 4.3:
            out.append(Finding("SECRET", m.start(), m.end(), 0.6))
    return out


_AKIA = re.compile(r"(?<![A-Za-z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Za-z0-9])")
_B64_40 = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{40}(?![A-Za-z0-9+/=])")


def _aws_pair(text: str) -> list[Finding]:
    """The 40-character secret that sits next to an AWS access key id (it has no shape of its own)."""
    out = []
    for a in _AKIA.finditer(text):
        window = text[a.end(): a.end() + 160]
        m = _B64_40.search(window)
        if m and any(c.isdigit() for c in m.group()) and any(c.isupper() for c in m.group()) and any(c.islower() for c in m.group()):
            out.append(Finding("SECRET", a.end() + m.start(), a.end() + m.end(), 0.8))
    return out


_PASSWORD_WORD = re.compile(r"(?i)pass|\u5bc6\u7801|\u53e3\u4ee4")


def _name_before(text: str, i: int) -> str:
    """The identifier characters right before position i (the prefix of `PublicKeyToken` when a pattern matched `Token`)."""
    j = i
    while j > max(0, i - 64) and (text[j - 1].isalnum() or text[j - 1] in "_-."):
        j -= 1
    return text[j:i]


def _secrets(text: str) -> list[Finding]:
    out = []
    for name, rx, score, value_only in _COMPILED_SECRETS:
        for m in rx.finditer(text):
            g = 1 if value_only else 0
            if m.start(g) < 0:
                continue
            if _is_placeholder_value(m.group(g)) and name in _TYPED_VALUES:
                continue
            if name == "azure_storage_key" and _AFTER_B64.search(text[max(0, m.start() - 24):m.start()]):
                continue
            v = m.group(g)
            if name == "secret_phrase" and len(v.rstrip(".,;:!?*/)")) < 8:
                continue                   # "the password is reset.*/": a word ending a sentence
            if name in {"credential_assignment", "secret_phrase", "secret_phrase_zh"}:
                # after a password word, people pick words plus digits (Password123, SummerTime2024)
                wordy = name == "secret_phrase" or bool(_PASSWORD_WORD.search(text, m.start(), m.start(g)))
                if _IDENT_CHAIN.fullmatch(v) or v.lower() in _CODE_WORDS or not _secretish(v, wordy_ok=wordy):
                    continue
                if name == "credential_assignment" and ("public" in _segments(_name_before(text, m.start()))
                                                        or _identifier_like(v)):
                    continue
            if name == "setter_literal" and (_IDENT_CHAIN.fullmatch(v) or v.startswith("/") or
                                             ("Pass" not in m.group(0) and not any(c.isdigit() for c in v))):
                continue                   # setToken('foo.bar.baz'), new Credential("/captain"), new Token("NUMBER")
            if name in _DUMMY_CHECKED:
                pw = v.rsplit(":", 1)[-1] if name == "curl_option" else v
                if v.lower() in _DUMMY or _self_described_dummy(pw) or _low_variety(pw, name in _PASSWORD_SHAPES) or len(pw) < 3:
                    continue
                if name == "cli_password" and (_CAMEL_WORDS.fullmatch(v) or _identifier_like(v) or v.endswith("]")
                                               or (v.isalpha() and v.isupper() and len(v) <= 12)):
                    continue               # help text (`--password  Force ...`, `--password TEXT`), an env var name
                if name == "oauth_param" and len(v) < 8:
                    continue
            if name in {"auth_header", "bearer_value"} and _SNAKE_OR_KEBAB.fullmatch(v) and not _UUID.fullmatch(v):
                continue                   # 'Basic login_and_password_removed': a name, not a credential
            out.append(Finding("SECRET", m.start(g), m.end(g), score))
    if ("=" in text or ":" in text) and len(text) > 12:
        out += _name_driven(text)
    out += _aws_pair(text)
    out += _entropy_tokens(text)
    return out


# ------------------------------------------------------------------------------------------------ personal data

_CJK = re.compile(r"[\u4e00-\u9fff]")
_COMPANY_WORDS = {"inc", "llc", "ltd", "limited", "corp", "corporation", "company", "co", "group", "holdings", "bank", "university", "institute",
                  "foundation", "labs", "lab", "technologies", "technology", "systems", "solutions", "partners", "services", "service", "industries",
                  "enterprises", "association", "agency", "studio", "studios", "media", "team", "department", "office", "support", "plc", "gmbh",
                  "sa", "ag", "bv", "pty", "council", "committee", "board", "club", "church", "school", "hospital", "clinic", "center", "centre"}

_W = r"[A-Z][A-Za-z\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u00ff'\u2019-]*[a-z\u00e0-\u00f6\u00f8-\u00ff]"
_PARTICLE = r"(?:de|del|della|di|da|van|von|der|den|bin|binti|al|el|le|la|ben|ibn|dos|das|du)"
_NAME = rf"{_W}(?:[ \t]+[A-Z]\.)?(?:[ \t]+(?:{_PARTICLE}[ \t]+)?{_W}){{1,3}}"
_TITLE = r"(?:(?:Mr|Mrs|Ms|Miss|Dr|Prof|Sir|Madam)\.?\s+)?"

_LABEL_COLON = (r"Name|Full name|Legal name|Party [A-D]|First party|Second party|Client|Customer|Provider|Contractor|Consultant|Tenant|Landlord|"
                r"Lessee|Lessor|Employee|Employer|Buyer|Seller|Borrower|Lender|Applicant|Patient|Guarantor|Signatory|Signed|Signature|"
                r"Printed name|Witness|Beneficiary|Insured|Policyholder|Cardholder|Account holder|Contact person|Contact|Recipient|"
                r"Owner|Representative|Sender|Resident|Student|Parent|Guardian|Spouse|Next of kin|Emergency contact|Licensee|Licensor|Grantor|"
                r"Grantee|Debtor|Creditor|Payee|Payer|Assignor|Assignee|Executor|Trustee|Trustor|Heir|Testator|Defendant|"
                r"Plaintiff|Claimant|Respondent|Petitioner|Subscriber|Sponsor|Donor|Candidate|Passenger|Traveler|Traveller|Guest|Attendee|"
                r"Participant|Interviewee|Referee|Shareholder")
_LABEL_FREE = (r"Resident|Signed by|Dear|Attn|Attention|c/o|Care of|Ship(?:ped)?(?: it)? to|Bill(?:ed)? to|Invoice to|Deliver(?:ed)?(?: it)? to|"
               r"(?:Send|Ship|Mail|Deliver|Forward)(?:\s+(?:it|them|this|that|the\s+\w+|a\s+\w+)){0,2}\s+to|Ship(?:ped)? to")
_EN_NAME_AFTER_LABEL = re.compile(
    rf"(?:^|[\s,;(])(?:(?i:{_LABEL_COLON})(?:\s*\([^)\n]{{0,40}}\))?\s*[:\-\u2013\u2014]|(?i:{_LABEL_FREE})\b(?:\s*[:\-\u2013\u2014])?)"
    rf"[ \t_.\u2026]*{_TITLE}(?P<name>{_NAME})")
_BETWEEN = re.compile(rf"(?i:\bbetween|\bby and between)\s+{_TITLE}(?P<n1>{_NAME})")
_AND_NAME = re.compile(rf"\band\s+{_TITLE}(?P<n2>{_NAME})")
_I_COMMA = re.compile(rf"(?:^|\n|\. )I,\s+(?P<name>{_NAME}),")


def _is_company(name: str) -> bool:
    return any(w.lower().strip(".,") in _COMPANY_WORDS for w in name.split())


def _en_names(text: str) -> list[Finding]:
    out = []
    for rx in (_EN_NAME_AFTER_LABEL, _I_COMMA):
        for m in rx.finditer(text):
            if not _is_company(m.group("name")):
                out.append(Finding("PERSON", m.start("name"), m.end("name"), 0.75))
    for m in _BETWEEN.finditer(text):
        if not _is_company(m.group("n1")):
            out.append(Finding("PERSON", m.start("n1"), m.end("n1"), 0.75))
        for m2 in _AND_NAME.finditer(text, m.end("n1"), m.end("n1") + 160):
            if "\n\n" in text[m.end("n1"):m2.start()] or _is_company(m2.group("n2")):
                continue
            out.append(Finding("PERSON", m2.start("n2"), m2.end("n2"), 0.7))
            break
    return out


_SURNAMES = ("\u738b\u674e\u5f20\u5218\u9648\u6768\u9ec4\u8d75\u5434\u5468\u5f90\u5b59\u9a6c\u6731\u80e1\u90ed\u4f55\u9ad8\u6797\u7f57\u90d1\u6881\u8c22\u5b8b\u5510\u8bb8\u97e9\u51af\u9093\u66f9"
             "\u5f6d\u66fe\u8427\u7530\u8463\u8881\u6f58\u4e8e\u848b\u8521\u4f59\u675c\u53f6\u7a0b\u82cf\u9b4f\u5415\u4e01\u4efb\u6c88\u59da\u5362\u59dc\u5d14\u949f\u8c2d\u9646\u6c6a"
             "\u8303\u91d1\u77f3\u5ed6\u8d3e\u590f\u97e6\u4ed8\u65b9\u767d\u90b9\u76db\u718a\u79e6\u90b1\u6c5f\u5c39\u859b\u960e\u6bb5\u96f7\u4faf\u9f99\u53f2\u9676\u9ece\u8d3a"
             "\u987e\u6bdb\u90dd\u9f9a\u90b5\u4e07\u94b1\u4e25\u8983\u6b66\u6234\u83ab\u5b54\u5411\u6c64\u5eb7\u8d56\u5e38\u4e01\u6613\u5eb7\u5170\u5173\u9a86\u5170")
_COMPOUND = ("\u6b27\u9633|\u53f8\u9a6c|\u4e0a\u5b98|\u8bf8\u845b|\u4e1c\u65b9|\u7687\u752b|\u5c09\u8fdf|\u516c\u5b59|\u6155\u5bb9|\u4ee4\u72d0|\u53f8\u5f92|\u590f\u4faf|"
             "\u6fb9\u53f0|\u957f\u5b59|\u72ec\u5b64|\u5357\u5bab|\u5b87\u6587|\u8f69\u8f95|\u7aef\u6728|\u62d3\u8dcb")
_ZH_NAME = rf"(?:(?:{_COMPOUND})[\u4e00-\u9fa5]{{1,2}}|[{_SURNAMES}][\u4e00-\u9fa5\u00b7]{{1,3}})"
_ZH_LABELS = ("\u53c2\u4f1a\u4eba\u5458|\u53c2\u4f1a\u4eba|\u4e0e\u4f1a\u4eba\u5458|\u4e0e\u4f1a\u8005|\u51fa\u5e2d\u4eba\u5458|\u51fa\u5e2d\u4eba|\u8bb0\u5f55\u4eba|\u4e3b\u6301\u4eba|"
              "\u6284\u9001|\u5ba1\u6279\u4eba|\u5ba1\u6838\u4eba|\u8d77\u8349\u4eba|\u53d1\u4ef6\u4eba|"
              "\u7532\u65b9|\u4e59\u65b9|\u4e19\u65b9|\u4e01\u65b9|\u51fa\u79df\u4eba|\u627f\u79df\u4eba|\u51fa\u79df\u65b9|\u627f\u79df\u65b9|\u51fa\u5356\u4eba|\u4e70\u53d7\u4eba|\u5356\u65b9|\u4e70\u65b9|"
              "\u59d4\u6258\u4eba|\u53d7\u6258\u4eba|\u51fa\u501f\u4eba|\u501f\u6b3e\u4eba|\u8d37\u6b3e\u4eba|\u62c5\u4fdd\u4eba|\u4fdd\u8bc1\u4eba|\u59d3\u540d|\u540d\u5b57|"
              "\u6cd5\u5b9a\u4ee3\u8868\u4eba|\u8054\u7cfb\u4eba|\u6536\u4ef6\u4eba|\u6536\u8d27\u4eba|\u6237\u4e3b|\u7acb\u7ea6\u4eba|\u7acb\u4e66\u4eba|\u7b7e\u7ea6\u4eba|\u7b7e\u5b57|\u7b7e\u540d|"
              "\u7533\u8bf7\u4eba|\u6295\u4fdd\u4eba|\u88ab\u4fdd\u9669\u4eba|\u53d7\u76ca\u4eba|\u60a3\u8005|\u75c5\u4eba|\u5458\u5de5|\u5b66\u751f|\u5bb6\u957f|\u76d1\u62a4\u4eba|"
              "\u7d27\u6025\u8054\u7cfb\u4eba|\u7ecf\u529e\u4eba|\u8d1f\u8d23\u4eba|\u4ee3\u7406\u4eba|\u672c\u4eba|\u6211\u53eb|\u540d\u53eb")
_ZH_NAME_FULL = re.compile(_ZH_NAME)
_ZH_NAME_AFTER_LABEL = re.compile(rf"(?:{_ZH_LABELS})(?:[\uff08(][^\uff09)\n]{{0,8}}[\uff09)])?[:\uff1a\s]*(?P<name>{_ZH_NAME})")
_ZH_LIST_NEXT = re.compile(rf"[\u3001\uff0c,/\s]*(?:\u548c|\u53ca|\u4e0e)?\s*(?P<name>{_ZH_NAME})(?=[\u3001\uff0c,/\s]|\u548c|\u53ca|\u4e0e|$)")
_ZH_LIST_SEP = re.compile(r"[\u3001\uff0c,/]|\s*(?:\u548c|\u53ca|\u4e0e)")
_ZH_LAZY_NAME = rf"(?:(?:{_COMPOUND})[\u4e00-\u9fa5]{{1,2}}?|[{_SURNAMES}][\u4e00-\u9fa5]{{1,2}}?)"
_ZH_NAME_OWNS = re.compile(rf"\u7531(?P<name>{_ZH_LAZY_NAME})(?=\u8d1f\u8d23|\u7275\u5934|\u4e3b\u5bfc|\u8ddf\u8fdb)")
_ZH_NAME_TITLE = re.compile(rf"(?P<name>(?:{_COMPOUND})[\u4e00-\u9fa5]{{0,1}}|[{_SURNAMES}][\u4e00-\u9fa5]{{0,2}}?)(?=\u5148\u751f|\u5973\u58eb|\u5c0f\u59d0)")
_ZH_ORG_AFTER = re.compile(r"[\u4e00-\u9fa5]{0,6}(?:\u516c\u53f8|\u96c6\u56e2|\u6709\u9650|\u79d1\u6280|\u94f6\u884c|\u5b66\u6821|\u533b\u9662|\u4e2d\u5fc3|\u5b66\u9662|\u5927\u5b66|\u5de5\u5382|"
                           r"\u5546\u5e97|\u4e8b\u52a1\u6240|\u7814\u7a76\u9662|\u534f\u4f1a|\u59d4\u5458\u4f1a|\u653f\u5e9c|\u8d85\u5e02|\u5e97)")


def _zh_names(text: str) -> list[Finding]:
    out = []
    for m in _ZH_NAME_AFTER_LABEL.finditer(text):
        name = m.group("name")
        if _ZH_ORG_AFTER.match(text, m.start("name")) or _ZH_ORG_AFTER.search(name):
            continue
        out.append(Finding("PERSON", m.start("name"), m.end("name"), 0.75))
        end = m.end("name")
        while (sep := _ZH_LIST_SEP.match(text, end)) and (nxt := _ZH_LIST_NEXT.match(text, end)) and sep.end() > end:
            if _ZH_ORG_AFTER.match(text, nxt.start("name")):
                break
            out.append(Finding("PERSON", nxt.start("name"), nxt.end("name"), 0.7))
            end = nxt.end("name")
    for m in _ZH_NAME_OWNS.finditer(text):
        out.append(Finding("PERSON", m.start("name"), m.end("name"), 0.7))
    for m in _ZH_NAME_TITLE.finditer(text):
        out.append(Finding("PERSON", m.start("name"), m.end("name"), 0.7))
    return out


# Names in tables: a header cell names the column (name, full name, contact, ...), and each later row with the same
# number of cells gives a person in that column. Claude Code shows files with line numbers ("     3\t" or "     3→"),
# so that prefix is dropped first. Values must look like names: two or more capitalized words (one is enough under a
# first / last name header) or a Chinese name.
_NAME_HEADERS = {"name", "full name", "fullname", "legal name", "display name", "person", "contact", "contact name",
                 "contact person", "employee", "employee name", "customer", "customer name", "client", "client name",
                 "owner", "owner name", "signatory", "recipient", "sender", "author", "member", "member name",
                 "姓名", "名字", "联系人", "员工", "员工姓名",
                 "客户姓名", "负责人"}
_PART_HEADERS = {"first name", "firstname", "given name", "last name", "lastname", "surname", "family name"}
_LINE_NO = re.compile(r"^[ \t]*\d+(?:\t|→)")
_CELL_NAME = re.compile(rf"{_TITLE}{_W}(?:[ \t]+(?:[A-Z]\.|{_PARTICLE}|{_W})){{1,3}}")
_CELL_PART = re.compile(_W)


def _cells(line: str, sep: str) -> list[tuple[int, int]]:
    """Cell spans of one delimited line (quotes respected, surrounding spaces and quotes trimmed)."""
    spans, start, quoted = [], 0, False
    for i, ch in enumerate(line + sep):
        if ch == '"':
            quoted = not quoted
        elif ch == sep and not quoted:
            a, b = start, i
            while a < b and line[a] in ' \t"':
                a += 1
            while b > a and line[b - 1] in ' \t"':
                b -= 1
            spans.append((a, b))
            start = i + 1
    return spans


def _row(line: str, sep: str) -> list[tuple[int, int]]:
    cells = _cells(line, sep)
    if sep == "|":   # a markdown row: drop the empty cells outside the outer bars
        if cells and cells[0][0] == cells[0][1]:
            cells = cells[1:]
        if cells and cells[-1][0] == cells[-1][1]:
            cells = cells[:-1]
    return cells


def _table_names(text: str) -> list[Finding]:
    out: list[Finding] = []
    lines, pos = [], 0
    for raw in text.split("\n"):
        m = _LINE_NO.match(raw)
        skip = m.end() if m else 0
        lines.append((pos + skip, raw[skip:]))
        pos += len(raw) + 1
    i = 0
    while i < len(lines):
        off, head = lines[i]
        i += 1
        sep = next((c for c in ",\t|;" if c in head), None)
        if sep is None:
            continue
        cols = _row(head, sep)
        if len(cols) < 2:
            continue
        keys = [re.sub(r"[\s_\-]+", " ", head[a:b]).strip().lower() for a, b in cols]
        wanted = {k: (key in _PART_HEADERS) for k, key in enumerate(keys) if key in _NAME_HEADERS | _PART_HEADERS}
        if not wanted:
            continue
        while i < len(lines):
            roff, row = lines[i]
            cells = _row(row, sep)
            if sep == "|" and cells and all(set(row[a:b]) <= set("-: ") for a, b in cells):
                i += 1   # markdown separator row
                continue
            if len(cells) != len(cols):
                break
            i += 1
            for k, part in wanted.items():
                a, b = cells[k]
                v = row[a:b]
                if (_ZH_NAME_FULL.fullmatch(v) or _CELL_NAME.fullmatch(v) or part and _CELL_PART.fullmatch(v)) \
                        and not _is_company(v):
                    out.append(Finding("PERSON", roff + a, roff + b, 0.75))
    return out


def column_persons(text: str, found: list[Finding]) -> list[Finding]:
    """Cells that share a column with a known person, in a block of two or more delimited rows with the same number of
    cells (script output without a header, `name | dept | salary`): the other cells of that column that look like
    names are persons too. `found` holds what the other detectors (and the placeholders) already named."""
    people = [(f.start, f.end) for f in found if f.entity == "PERSON"]
    if not people or "\n" not in text:
        return []
    out: list[Finding] = []
    lines, pos = [], 0
    for raw in text.split("\n"):
        m = _LINE_NO.match(raw)
        skip = m.end() if m else 0
        lines.append((pos + skip, raw[skip:]))
        pos += len(raw) + 1
    i = 0
    while i < len(lines):
        sep = next((c for c in "|\t,;" if c in lines[i][1]), None)
        n = len(_row(lines[i][1], sep)) if sep else 0
        j = i
        while sep and j < len(lines) and len(_row(lines[j][1], sep)) == n and n >= 2:
            j += 1
        if j - i >= 2:
            rows = [[(off + a, off + b) for a, b in _row(line, sep)] for off, line in lines[i:j]]
            for k in range(n):
                col = [r[k] for r in rows]
                if not any(a <= s0 and e0 <= b and (s0, e0) != (a, a) and text[s0:e0].strip() for a, b in col
                           for s0, e0 in people if a <= s0 < b):
                    continue
                for a, b in col:
                    v = text[a:b]
                    if (_ZH_NAME_FULL.fullmatch(v) or _CELL_NAME.fullmatch(v)) and not _is_company(v) \
                            and not any(s0 < b and a < e0 for s0, e0 in people):
                        out.append(Finding("PERSON", a, b, 0.7))
        i = max(j, i + 1)
    out += _space_rows(text, lines, people)
    return out


def _space_rows(text: str, lines: list[tuple[int, str]], people: list[tuple[int, int]]) -> list[Finding]:
    """Rows separated by spaces only (`print(name, dept, salary)`): a person at the start of a row, followed by k more
    tokens, makes the leading part of each neighbouring row (all but its last k tokens) a person when it looks like
    a name. Neighbours run up to a blank line."""
    out: list[Finding] = []
    offs = [off for off, _ in lines]
    for s0, e0 in people:
        n = bisect.bisect_right(offs, s0) - 1
        if n < 0:
            continue
        off, line = lines[n]
        if s0 - off > len(line) or line[:s0 - off].strip():
            continue
        rest = len(text[e0:off + len(line)].split())
        if rest < 1 or len(line.split()) < 2:
            continue
        for step in (-1, 1):
            m = n + step
            while 0 <= m < len(lines) and lines[m][1].strip():
                roff, row = lines[m]
                toks = list(re.finditer(r"\S+", row))
                if len(toks) > rest:
                    a, b = toks[0].start(), toks[-rest - 1].end()
                    v = row[a:b]
                    if (_ZH_NAME_FULL.fullmatch(v) or _CELL_NAME.fullmatch(v)) and not _is_company(v) \
                            and not any(x < roff + b and roff + a < y for x, y in people):
                        out.append(Finding("PERSON", roff + a, roff + b, 0.65))
                m += step
    return out


# --- ID numbers

_CN_ID = re.compile(r"(?<![0-9])[1-9]\d{5}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[0-9Xx](?![0-9])")
_CN_MOBILE = re.compile(r"(?<![0-9.])(?:\+86[- ]?|86[- ])?1[3-9]\d{9}(?![0-9]|\.\d)")
_PHONE_WORDS = re.compile(r"(?i)tel|phone|mobile|cell|call|whatsapp|wechat|contact|reach|text me|sms|[\u4e00-\u9fff]|\+86\s*$")
_HK_ID = re.compile(r"(?<![A-Za-z0-9])[A-Z]{1,2}\d{6}\([0-9A]\)")
_TW_ID = re.compile(r"(?<![A-Za-z0-9])[A-Z][12]\d{8}(?![0-9])")
_PAN = re.compile(r"(?<![A-Za-z0-9])[A-Z]{5}\d{4}[A-Z](?![A-Za-z0-9])")
_AADHAAR = re.compile(r"(?<![\d-])(?<!\d )[2-9]\d{3}[ -]\d{4}[ -]\d{4}(?![\d]|[ -]\d)")
_KR_RRN = re.compile(r"(?<![\d-])\d{6}-[1-4]\d{6}(?![\d-])")
_UK_NI = re.compile(r"(?<![A-Za-z0-9])(?!BG|GB|NK|KN|TN|NT|ZZ)[A-CEGHJ-PR-TW-Z][A-CEGHJ-NPR-TW-Z] ?\d{2} ?\d{2} ?\d{2} ?[A-D](?![A-Za-z0-9])")
_ID_LABEL = re.compile(
    r"(?<![A-Za-z])(?i:passport|id card|identity card|national id|national insurance|nino|ni number|id no\.?|id number|identification number|"
    r"driver'?s?[ -]licen[cs]e|licen[cs]e no\.?|tax id|tin|ssn|sin|social security|resident id|visa|"
    r"\u62a4\u7167|\u8bc1\u4ef6|\u8eab\u4efd\u8bc1|\u5c45\u6c11\u8eab\u4efd\u8bc1|\u9a7e\u9a76\u8bc1|\u793e\u4fdd|\u5de5\u5361)"
    r"(?![A-Za-z])(?:\s*(?i:no\.?|number|num|#|\u53f7\u7801|\u53f7|\u7f16\u53f7))?\s*[:\uff1a#]?\s*"
    r"(?P<v>(?=[A-Za-z0-9 -]{0,20}\d)[A-Za-z0-9]{6,14}(?![A-Za-z0-9])(?:[ -]\d{2,6}(?!\d)){0,3})")
_CARD_WORDS = re.compile(r"(?i)card|visa|master|amex|bank|account|acct|iban|pay|transfer|debit|credit|\u5361|\u94f6\u884c|\u8d26|\u8f6c\u8d26|\u4ed8\u6b3e|\u6536\u6b3e")
_BANK_CARD = re.compile(r"(?<![0-9])\d{16,19}(?![0-9])")
_TW_LETTERS = "ABCDEFGHJKLMNPQRSTUVXYWZIO"


def _luhn_ok(s: str) -> bool:
    digits = [int(c) for c in s][::-1]
    return sum(d if i % 2 == 0 else (d * 2 - 9 if d * 2 > 9 else d * 2) for i, d in enumerate(digits)) % 10 == 0


def _tw_ok(s: str) -> bool:
    n = _TW_LETTERS.index(s[0]) + 10
    digits = [int(c) for c in s[1:]]
    total = n // 10 + (n % 10) * 9 + sum(d * w for d, w in zip(digits[:8], range(8, 0, -1))) + digits[8]
    return total % 10 == 0


_CN_W = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]


def _cn_ok(s: str) -> bool:
    s = s.upper()
    return "10X98765432"[sum(int(c) * w for c, w in zip(s[:17], _CN_W)) % 11] == s[17]


def _ids(text: str) -> list[Finding]:
    out = []
    for m in _CN_ID.finditer(text):
        if _cn_ok(m.group()):
            out.append(Finding("CN_ID_CARD", m.start(), m.end(), 0.85))
    for m in _CN_MOBILE.finditer(text):
        if m.group().startswith(("+86", "86")) or _PHONE_WORDS.search(text[max(0, m.start() - 24):m.start()]):
            out.append(Finding("CN_MOBILE", m.start(), m.end(), 0.7))
    for m in _BANK_CARD.finditer(text):
        if _luhn_ok(m.group()) and _CARD_WORDS.search(text[max(0, m.start() - 48):m.start()]):
            out.append(Finding("BANK_CARD", m.start(), m.end(), 0.6))
    for m in _TW_ID.finditer(text):
        if _tw_ok(m.group()):
            out.append(Finding("NATIONAL_ID", m.start(), m.end(), 0.8))
    for rx in (_HK_ID, _PAN, _AADHAAR, _KR_RRN, _UK_NI):
        for m in rx.finditer(text):
            out.append(Finding("NATIONAL_ID", m.start(), m.end(), 0.7))
    for m in _ID_LABEL.finditer(text):
        v = m.group("v")
        if not _is_placeholder_value(v) and any(c.isdigit() for c in v):
            out.append(Finding("NATIONAL_ID", m.start("v"), m.end("v"), 0.7))
    return out


# --- addresses

_SUFFIX = (r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Court|Ct|Way|Place|Pl|Terrace|Ter|Highway|Hwy|Parkway|Pkwy|"
           r"Square|Sq|Circle|Cir|Close|Crescent|Cres|Gardens|Row|Alley|Trail|Walk|Mews)")
_ROMANCE = r"(?:Rue|Via|Viale|Calle|Avenida|Rua|Carrer|Strada|Chemin|Allée|Boulevard|Avenue|Place|Plaza|Piazza)"
_STREET_EN = re.compile(
    rf"(?:(?:Flat|Apt|Apartment|Unit|Suite|Ste|Room|Rm)\.?\s*[\w-]+,\s*)?"
    rf"(?:\b\d{{1,6}}[A-Za-z]?(?:-\d{{1,6}})?\s+(?:[A-Z0-9][\w.'\u2019-]*\s+){{1,4}}?{_SUFFIX}\b\.?"
    rf"|\b\d{{1,6}},?\s+{_ROMANCE}\s+(?:(?:de|du|des|la|le|les|del|della|di|dei|da|do|dos|das|d')\s*)*[A-Z][\w'\u2019-]+(?:\s+[A-Z][\w'\u2019-]+){{0,3}}"
    rf"|\b[A-Z][a-z\u00df\u00e4\u00f6\u00fc]+(?:stra\u00dfe|strasse|straat|gasse|weg|platz|allee|laan)\s+\d{{1,4}}[a-z]?)")
_TAIL_TOKEN = re.compile(r"(?:[A-Z][A-Za-z.'\u2019-]*|\d[\dA-Za-z-]*|[A-Z]{1,2}\d[A-Z\d]?|[A-Z0-9]{2,8}|(?:de|du|la|le|upon|on|of|the)(?=\s+[A-Z]))$")
_COUNTRY_OR_PLACE_STOP = {"And", "The", "Or", "But", "If", "He", "She", "They", "Please", "Thanks", "Thank", "Regards", "Best", "Dear"}


def _extend_address(text: str, end: int) -> int:
    """Take the following comma-separated parts (unit, city, state / province, postal code) while they look like address
    parts: capitalized words or codes, at most five tokens each, at most four parts."""
    pos = end
    for _ in range(4):
        m = re.match(r",?\s*([^,\n;.]{1,40})", text[pos:pos + 60]) if text[pos:pos + 1] == "," else None
        if not m:
            break
        seg = m.group(1).strip()
        toks = seg.split()
        if not toks or len(toks) > 5 or toks[0] in _COUNTRY_OR_PLACE_STOP:
            break
        if not all(_TAIL_TOKEN.match(t) for t in toks):
            break
        pos += m.end()
    # the last part may run on without a comma before a postal code: "Denver CO 80203", "Taipei 10041"
    return pos


_ADDR_LABEL_EN = re.compile(
    r"(?im)(?<!\bip )(?<!\bmac )(?<!\be-mail )(?<!\bemail )(?<!\bweb )(?<!\bwallet )(?<!\bcontract )(?<!\breturn )(?<!\bmemory )(?<!\bblock )"
    r"\b(?:(?:home|mailing|billing|shipping|delivery|residential|permanent|current|registered|street|postal|business|work|office|correspondence)\s+)?"
    r"(?:address(?:es)?|residence)\s*[:\-\u2013\uff1a]\s*(?P<v>[^\n]{6,120})")
_NOT_ADDRESS_VALUE = re.compile(r"^(?:(?:\d{1,3}\.){3}\d{1,3}|[\w.+-]+@[\w.-]+|https?://\S+|0x[0-9a-fA-F]+|[0-9a-fA-F:]{12,})$")


def _en_addresses(text: str) -> list[Finding]:
    out = []
    for m in _STREET_EN.finditer(text):
        end = _extend_address(text, m.end())
        # "Denver CO 80203": city / state / postal code written without commas after the last comma part
        tail = re.match(r"[ \t]+(?:[A-Z]{2}[ \t]+\d{5}(?:-\d{4})?|[A-Z]{1,2}\d[A-Z\d]?[ \t]*\d[A-Z]{2}|\d{4,6})", text[end:end + 20])
        if tail:
            end += tail.end()
        out.append(Finding("ADDRESS", m.start(), end, 0.75))
    for m in _ADDR_LABEL_EN.finditer(text):
        v = m.group("v").strip().rstrip(".;,")
        if len(v.split()) < 2 or not (any(c.isdigit() for c in v) and any(c.isalpha() for c in v)):
            continue
        if _NOT_ADDRESS_VALUE.match(v) or _is_placeholder_value(v) or re.search(r"\[|\bOptional\b|\bstr\b|=>|\(\)", v):
            continue
        out.append(Finding("ADDRESS", m.start("v"), m.start("v") + len(v), 0.75))
    return out


_ZH_PLACE_UNIT = (r"(?:[\u4e00-\u9fa5A-Za-z0-9]{0,6}(?:\d+\u53f7\u9662?|\u53f7\u697c|\u680b|\u5e62|\u5ea7|\u5355\u5143|\u5ba4|\u5c42|\u697c|\u95e8|\u9662|\u53f7)[A-Za-z\d\-]*)")
_ADDR_ZH = re.compile(
    r"(?:[\u4e00-\u9fa5]{2,8}(?:\u7701|\u81ea\u6cbb\u533a|\u7279\u522b\u884c\u653f\u533a))?"
    r"(?:[\u4e00-\u9fa5]{2,8}(?:\u5e02|\u81ea\u6cbb\u5dde|\u5730\u533a|\u76df))?"
    r"(?:[\u4e00-\u9fa5]{1,8}(?:\u533a|\u53bf|\u65d7|\u5e02))?"
    r"[\u4e00-\u9fa5A-Za-z0-9]{1,16}(?:\u8def|\u8857|\u5927\u8857|\u5927\u9053|\u9053|\u5df7|\u5f04|\u80e1\u540c|\u6751|\u9547|\u4e61|\u5c6f|\u91cc|\u56ed|\u82d1|\u5e7f\u573a|\u5927\u53a6|\u5c0f\u533a|\u793e\u533a)"
    rf"{_ZH_PLACE_UNIT}*")
_ADDR_LABEL_ZH = re.compile(
    r"(?:\u4f4f\u5740|\u5730\u5740|\u4f4f\u6240\u5730?|\u6237\u7c4d\u5730\u5740?|\u901a\u8baf\u5730\u5740|\u8054\u7cfb\u5730\u5740|\u6536\u8d27\u5730\u5740|\u5bb6\u5ead\u4f4f\u5740|\u73b0\u4f4f\u5740|"
    r"\u5c45\u4f4f\u5730|\u73b0\u5c45\u4f4f\u5730?|\u529e\u516c\u5730\u5740|\u6ce8\u518c\u5730\u5740|\u7ecf\u8425\u5730\u5740|\u5e38\u4f4f\u5730\u5740)[:\uff1a\s]*(?P<v>[^\n\uff0c\u3002\uff1b;]{4,60})")
_ZH_PLACE_HINT = re.compile(r"[\u7701\u5e02\u533a\u53bf\u8def\u8857\u9053\u5df7\u53f7\u6751\u9547\u4e61\u697c\u5ba4\u680b\u5c42\u5355\u5143\u9662\u56ed\u82d1\u5927\u53a6\d]")


def _zh_addresses(text: str) -> list[Finding]:
    out = []
    for m in _ADDR_ZH.finditer(text):
        s = m.group()
        regions = len(re.findall(r"[\u7701\u5e02\u533a\u53bf\u65d7]", s))
        if any(c.isdigit() for c in s) or regions >= 2:
            out.append(Finding("ADDRESS", m.start(), m.end(), 0.75))
    for m in _ADDR_LABEL_ZH.finditer(text):
        v = m.group("v").strip()
        if _ZH_PLACE_HINT.search(v) and len(v) >= 4:
            out.append(Finding("ADDRESS", m.start("v"), m.start("v") + len(v), 0.75))
    return out


def _pii(text: str) -> list[Finding]:
    out = []
    has_cjk = bool(_CJK.search(text))
    if has_cjk:
        out += _zh_names(text) + _zh_addresses(text)
    if any(c.isdigit() for c in text):
        out += _ids(text)
        out += _en_addresses(text)
    if any(c.isupper() for c in text):
        out += _en_names(text)
    if "\n" in text:
        out += _table_names(text)
    return out


def find(text: str) -> list[Finding]:
    """Every credential and personal-data span the regex layer can name, possibly overlapping (the caller merges)."""
    if len(text) < 6:
        return []
    return _secrets(text) + _pii(text)
