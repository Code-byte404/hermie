"""End-to-end canary suite (evals/e2e): generator, canary search, recorder, tasks, runner, scorer. No network."""
import json
import re
import subprocess
import sys
from itertools import permutations
from pathlib import Path

import pytest

from evals.e2e import workspace as W

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("ws") / "w"
    return out, W.build(1, out)


def _files(out: Path) -> dict[str, bytes]:
    return {str(p.relative_to(out)): p.read_bytes() for p in sorted(out.rglob("*")) if p.is_file()}


def test_build_is_deterministic(tmp_path):
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    W.build(7, a); W.build(7, b); W.build(8, c)
    assert _files(a) == _files(b)
    assert (a / "canaries.json").read_text() != (c / "canaries.json").read_text()


def test_every_canary_appears_in_its_file(built):
    out, canaries = built
    assert canaries == W.load_canaries(out / "canaries.json")
    for c in canaries:
        assert c.value in (out / c.file).read_text(encoding="utf-8"), c.id


def test_no_canary_contains_another(built):
    _, canaries = built
    assert len({c.value for c in canaries}) == len(canaries) == len({c.id for c in canaries})
    norm = [(c.id, W.normalize(c.value)) for c in canaries]
    for (ia, a), (ib, b) in permutations(norm, 2):
        assert a not in b, (ia, ib)


def test_kinds_and_languages_are_covered(built):
    _, canaries = built
    assert {c.kind for c in canaries} == set(W.KINDS)
    for c in canaries:
        assert c.claimed == W.KINDS[c.kind]
    for kind in ("person", "phone", "address", "api_key", "company", "amount"):
        assert {c.lang for c in canaries if c.kind == kind} >= {"en", "zh"}, kind


def test_key_canaries_use_provider_shapes(built):
    _, canaries = built
    keys = [c for c in canaries if c.kind == "api_key"]
    for prefix in ("sk-ant-api03-", "AKIA", "ghp_", "xoxb-"):
        assert any(c.value.startswith(prefix) for c in keys), prefix
    files = {c.file for c in keys}
    assert "app/.env" in files and files & {"company/meeting_notes_zh.md", "mail/forwarded_en.eml"}


def test_refuses_repo_and_nonempty_paths(tmp_path):
    with pytest.raises(ValueError):
        W.build(1, Path(__file__).parent / "e2e-out")
    (tmp_path / "full").mkdir()
    (tmp_path / "full" / "x").write_text("x")
    with pytest.raises(ValueError):
        W.build(1, tmp_path / "full")


def test_no_cjk_in_repo_sources():
    for p in (ROOT / "evals" / "e2e").rglob("*"):
        if p.is_file() and p.suffix in (".py", ".json", ".md", ".txt"):
            assert not re.search(r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]", p.read_text(encoding="utf-8")), p


def test_app_bug_is_real(built):
    out, _ = built
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=out / "app",
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 1, r.stdout[-500:]


def test_top_three_salaries_are_fixed_positions(built):
    out, canaries = built
    by_id = {c.id: c for c in canaries}
    rows = (out / "company" / "employees.csv").read_text(encoding="utf-8").splitlines()[1:]
    salaries = sorted((int(r.split(",")[4]), i) for i, r in enumerate(rows))
    assert [i for _, i in salaries[-3:]][::-1] == list(W.TOP_PAID)
    assert all(f"emp.{i}.name" in by_id for i in W.TOP_PAID)


# ---------------------------------------------------------------- canary search

from evals.e2e import leaks as L


def _c(cid, value, kind="person", lang="en"):
    return W.Canary(cid, value, kind, "f", lang, W.KINDS[kind])


def _body(obj, ascii_=True):
    return json.dumps(obj, ensure_ascii=ascii_).encode()


def test_finds_exact_and_reports_path():
    c = _c("p", "Clara Petrov")
    body = _body({"messages": [{"role": "user", "content": [{"type": "text", "text": "ask Clara Petrov today"}]}]})
    assert L.find_leaks(body, [c]) == [L.Leak("p", "$.messages[0].content[0].text", "exact")]


def test_finds_json_escaped_chinese():
    c = _c("z", "秦宇轩", lang="zh")
    for ascii_ in (True, False):
        assert [x.canary_id for x in L.find_leaks(_body({"m": "房东 " + c.value}, ascii_), [c])] == ["z"]
    nested = json.dumps({"path": "a.md", "content": "房东" + c.value}, ensure_ascii=True)
    assert [x.canary_id for x in L.find_leaks(_body({"input": nested}), [c])] == ["z"]


def test_finds_normalized_phone_and_card():
    phone = _c("ph", "(303) 555-0170", "phone")
    card = _c("cd", "6200001234567890123", "bank_card")
    found = L.find_leaks(_body({"t": "call 303 5550170 or pay 6200 0012 3456 7890 123"}), [phone, card])
    assert {(x.canary_id, x.form) for x in found} == {("ph", "normalized"), ("cd", "normalized")}


def test_finds_urlencoded_and_key_substring():
    pw = _c("pw", "Harborviolet!42xY7q", "password")
    key = _c("k", "ghp_" + "aB3dE5gH7jK9mN1pQ3sT5vX7zA9cE1gI3kM5", "api_key")
    who = _c("n", "Clara Petrov")
    body = _body({"u": "https://x/?p=Harborviolet%2142xY7q", "t": "token ends " + key.value[10:24],
                  "s": "Petrov"})
    assert {(x.canary_id, x.form) for x in L.find_leaks(body, [pw, key, who])} == {("pw", "urlencoded"),
                                                                                    ("k", "substring")}


def test_placeholder_is_not_a_leak():
    c = _c("p", "Clara Petrov")
    assert L.find_leaks(_body({"t": "ask <PERSON_1> today"}), [c]) == []


def test_short_values_need_word_bounds():
    c = _c("a", "4200", "amount")
    assert L.find_leaks(_body({"t": "order 142007 shipped"}), [c]) == []
    assert [x.form for x in L.find_leaks(_body({"t": "pay 4200 now"}), [c])] == ["exact"]


def test_one_leak_per_canary_and_path_strongest_form_first():
    c = _c("ph", "(303) 555-0170", "phone")
    found = L.find_leaks(_body({"t": "(303) 555-0170 and 3035550170"}), [c])
    assert found == [L.Leak("ph", "$.t", "exact")]


def test_non_json_body_is_searched_as_text():
    c = _c("p", "Clara Petrov")
    assert L.find_leaks(b"not json: Clara Petrov", [c]) == [L.Leak("p", "$", "exact")]


# ---------------------------------------------------------------- recorder

import asyncio

import httpx

from evals.e2e import recorder as R

FIX = ROOT / "tests" / "fixtures"


def _rec_client(tmp_path, handler):
    app = R.create_recorder("https://up.test/base", tmp_path / "rec", client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler)))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://rec")


async def test_records_body_not_headers(tmp_path):
    seen = []

    def up(req):
        seen.append(req)
        return httpx.Response(200, json={"ok": True})
    async with _rec_client(tmp_path, up) as c:
        body = json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode()
        r = await c.post("/v1/messages?beta=true", content=body,
                         headers={"x-api-key": "k-secret-test", "authorization": "Bearer k-secret-test"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert str(seen[0].url) == "https://up.test/base/v1/messages?beta=true"
    assert seen[0].headers["x-api-key"] == "k-secret-test" and seen[0].headers["authorization"].endswith("k-secret-test")
    assert seen[0].headers["host"] == "up.test"
    assert (tmp_path / "rec" / "001.json").read_bytes() == body
    assert json.loads((tmp_path / "rec" / "001.meta.json").read_text())["path"] == "/v1/messages"
    for p in (tmp_path / "rec").iterdir():
        assert b"k-secret-test" not in p.read_bytes()


def _free_port():
    import socket
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        return so.getsockname()[1]


def test_sse_passes_through_in_chunks(tmp_path):
    """A real server: httpx's ASGI transport buffers whole responses, so it cannot show streaming."""
    import threading
    import time
    blocks = [b + b"\n\n" for b in (FIX / "streams" / "anthropic.sse").read_bytes().split(b"\n\n") if b]
    first_seen = threading.Event()

    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield blocks[0]
            t0 = time.monotonic()
            while not first_seen.is_set():   # a buffering recorder never lets the client see the first block
                assert time.monotonic() - t0 < 5, "client never saw the first block"
                await asyncio.sleep(0.01)
            for b in blocks[1:]:
                yield b

    up = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Slow())))
    port = _free_port()
    with R.serve_recorder("https://up.test", tmp_path / "rec", port, client=up):
        got = b""
        with httpx.stream("POST", f"http://127.0.0.1:{port}/v1/messages", json={"stream": True}, timeout=10) as r:
            for chunk in r.iter_raw():
                got += chunk
                first_seen.set()
    assert got == b"".join(blocks)
    assert r.headers["content-type"].startswith("text/event-stream")
    assert json.loads((tmp_path / "rec" / "001.json").read_text()) == {"stream": True}


async def test_get_and_query_forwarded_and_numbered(tmp_path):
    async with _rec_client(tmp_path, lambda req: httpx.Response(200, json={"m": req.method})) as c:
        assert (await c.get("/v1/models?x=1")).json() == {"m": "GET"}
        assert (await c.post("/v1/messages", json={"a": 1})).json() == {"m": "POST"}
    assert (tmp_path / "rec" / "001.json").read_bytes() == b""
    assert json.loads((tmp_path / "rec" / "002.json").read_text()) == {"a": 1}


async def test_upstream_error_status_passes_through(tmp_path):
    async with _rec_client(tmp_path, lambda req: httpx.Response(529, json={"error": "overloaded"})) as c:
        r = await c.post("/v1/messages", json={})
    assert r.status_code == 529 and r.json() == {"error": "overloaded"}
    assert json.loads((tmp_path / "rec" / "001.meta.json").read_text())["status"] == 529
