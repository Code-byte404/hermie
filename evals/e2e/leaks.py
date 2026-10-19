"""Search a recorded request body for canaries. The body is parsed as JSON and every string leaf and dict key is
searched (Chinese arrives JSON-escaped or raw; both are found after parsing). A string leaf that is itself JSON (tool
arguments) is parsed and walked too. Forms, strongest first:

- exact: the value as is (ASCII values need non-alphanumeric neighbours, so 4200 is not found inside 142007);
- urlencoded: the percent-encoded value;
- normalized: `normalize(value)` inside `normalize(leaf)`, for values whose normalized form has 6+ characters
  (phones, cards and ids written with other spacing);
- part: for an address, its street line (English) or road and number (Chinese), found exact or normalized;
- substring: any 12-character slice of the random part (a provider prefix like `sk-ant-api03-` is skipped), only for
  key, password and URL-credential values of 16+ characters.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import quote, quote_plus

from evals.e2e.workspace import ZH, Canary, normalize

FRAGMENT_KINDS = ("api_key", "password", "url_credentials")
KEY_PREFIXES = ("sk-ant-api03-", "xoxb-", "ghp_", "AKIA")
FRAGMENT = 12
_ESCAPED = re.compile(r"\\u([0-9a-fA-F]{4})")


@dataclass(frozen=True)
class Leak:
    canary_id: str
    path: str
    form: str


def _leaves(obj, path: str = "$"):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield f"{path}.{k}#key", k
            yield from _leaves(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _leaves(v, f"{path}[{i}]")
    elif isinstance(obj, str):
        yield path, obj
        inner = None
        if obj.strip()[:1] in "{[":
            try:
                inner = json.loads(obj)
            except ValueError:
                pass
        if isinstance(inner, (dict, list)):
            yield from _leaves(inner, path + "<json>")
        elif "\\u" in obj:
            yield path, _ESCAPED.sub(lambda m: chr(int(m.group(1), 16)), obj)


def _exact(value: str, text: str) -> bool:
    if not value.isascii():
        return value in text
    for m in re.finditer(re.escape(value), text):
        before = text[m.start() - 1] if m.start() else " "
        after = text[m.end()] if m.end() < len(text) else " "
        if not ((value[0].isalnum() and before.isalnum()) or (value[-1].isalnum() and after.isalnum())):
            return True
    return False


def _parts(c: Canary) -> list[str]:
    if c.kind != "address":
        return []
    if c.value.isascii():
        street = c.value.split(",")[0].strip()
        return [street] if street != c.value and len(street) >= 8 else []
    for city, district in ZH["cities"]["items"]:
        if c.value.startswith(city + district):
            rest = c.value[len(city + district):]
            road = next((r for r in ZH["roads"]["items"] if rest.startswith(r)), None)
            m = re.match(re.escape(road) + r"\d+", rest) if road else None
            return [rest] + ([m.group()] if m else [])
    return []


def _part_found(part: str, text: str) -> bool:
    if part.isascii():
        return _exact(part, text) or (len(normalize(part)) >= 6 and normalize(part) in normalize(text))
    return re.search(re.escape(part) + (r"(?!\d)" if part[-1].isdigit() else ""), text) is not None


def _form(c: Canary, text: str) -> str | None:
    if _exact(c.value, text):
        return "exact"
    if any(e != c.value and e in text for e in (quote(c.value, safe=""), quote_plus(c.value, safe=""))):
        return "urlencoded"
    n = normalize(c.value)
    if len(n) >= 6 and n in normalize(text):
        return "normalized"
    if any(_part_found(p, text) for p in _parts(c)):
        return "part"
    if c.kind in FRAGMENT_KINDS and len(c.value) >= 16:
        body = next((c.value[len(p):] for p in KEY_PREFIXES if c.value.startswith(p)), c.value)
        if any(body[i:i + FRAGMENT] in text for i in range(len(body) - FRAGMENT + 1)):
            return "substring"
    return None


_RANK = {"exact": 0, "urlencoded": 1, "normalized": 2, "part": 3, "substring": 4}


def find_leaks(body: bytes, canaries: list[Canary]) -> list[Leak]:
    text = body.decode("utf-8", errors="replace")
    try:
        obj = json.loads(text)
    except ValueError:
        obj = text
    best: dict[tuple[str, str], str] = {}
    for path, leaf in _leaves(obj) if not isinstance(obj, str) else [("$", obj)]:
        for c in canaries:
            form = _form(c, leaf)
            key = (c.id, path)
            if form and (key not in best or _RANK[form] < _RANK[best[key]]):
                best[key] = form
    return [Leak(cid, path, form) for (cid, path), form in best.items()]
