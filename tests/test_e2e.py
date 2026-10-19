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


# ---------------------------------------------------------------- tasks and grading

from evals.e2e import grade as G
from evals.e2e import tasks as T


def test_every_task_refers_to_existing_canaries(built):
    _, canaries = built
    by_id = {c.id: c for c in canaries}
    claimed = 0
    for t in T.TASKS:
        for cid in t.must_mention + t.must_not_mention + tuple(G.rubric_ids(t.rubric)):
            assert cid in by_id, (t.id, cid)
        claimed += any(by_id[c].claimed for c in t.must_mention)
    assert claimed >= 0.7 * len(T.TASKS)


def test_task_mix():
    ids = [t.id for t in T.TASKS]
    assert len(ids) == len(set(ids)) and 18 <= len(ids) <= 22
    zh = sum(t.lang == "zh" for t in T.TASKS)
    assert 0.4 <= zh / len(ids) <= 0.6
    assert any(t.followups for t in T.TASKS)
    assert sum(bool(t.must_not_mention) for t in T.TASKS) >= 2
    assert any(t.run_tests for t in T.TASKS)
    assert all(t.prompt.strip() and t.rubric.strip() for t in T.TASKS)


def _task(**kw):
    base = dict(id="t", lang="en", prompt="p", followups=(), must_mention=("a",), must_not_mention=("b",),
                run_tests=False, rubric="names {a}")
    return T.Task(**(base | kw))


def test_check_finds_missing_wrong_and_placeholders(tmp_path):
    cs = {"a": _c("a", "Clara Petrov"), "b": _c("b", "Owen Brennan")}
    ok = G.check(_task(), "Ask clara petrov.", cs, tmp_path)
    assert ok["passed"] and ok["missing"] == [] and ok["tests_passed"] is None
    bad = G.check(_task(), "Ask <PERSON_2> and Owen Brennan.", cs, tmp_path)
    assert not bad["passed"] and bad["missing"] == ["a"] and bad["wrong"] == ["b"]
    assert bad["placeholders_left"] == ["<PERSON_2>"]


def test_check_runs_app_tests(built, tmp_path):
    import shutil
    out, canaries = built
    shutil.copytree(out, tmp_path / "w")
    r = G.check(_task(must_mention=(), must_not_mention=(), run_tests=True), "done", {}, tmp_path / "w")
    assert r["tests_passed"] is False and not r["passed"]
    code = tmp_path / "w" / "app" / "inventory.py"
    code.write_text(code.read_text().replace("len(items) - 1", "len(items)"))
    assert G.check(_task(must_mention=(), must_not_mention=(), run_tests=True), "done", {}, tmp_path / "w")["passed"]


def test_judge_prompt_is_blind_and_verdict_parses():
    cs = {"a": _c("a", "Clara Petrov")}
    key = G.render(_task().rubric, cs)
    assert key == "names Clara Petrov"
    prompt = G.judge_prompt(_task(), key, "answer text")
    assert "hermie" not in prompt.lower() and "direct" not in prompt.lower() and "answer text" in prompt
    assert G.parse_verdict("PASS\nok") == (True, "ok")
    assert G.parse_verdict("FAIL\nwrong party") == (False, "wrong party")
    assert G.parse_verdict("maybe")[0] is False


def test_passed_combines_checks_and_verdict():
    assert G.passed({"passed": True}, None) and G.passed({"passed": True}, (True, ""))
    assert not G.passed({"passed": True}, (False, "x")) and not G.passed({"passed": False}, (True, ""))


# ---------------------------------------------------------------- runner

import os
import threading

from evals.e2e import run as RUN

FAKE_CLAUDE = '''#!{python}
"""Stands in for claude -p: sends one request with the prompt and a workspace file, prints a claude-style result."""
import json, os, sys, urllib.request, urllib.error
if "--version" in sys.argv:
    print("9.9.9 (Claude Code)"); sys.exit(0)
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\\n")
if os.environ.get("FAKE_CLAUDE_FAIL"):
    print(json.dumps({{"type": "result", "is_error": True, "result": "API Error", "session_id": "s1"}})); sys.exit(1)
prompt = sys.argv[sys.argv.index("-p") + 1]
body = {{"model": "claude-test", "max_tokens": 10, "messages": [{{"role": "user", "content": prompt + "\\n" +
        open("contacts/clients.csv", encoding="utf-8").read()}}]}}
req = urllib.request.Request(os.environ["ANTHROPIC_BASE_URL"].rstrip("/") + "/v1/messages",
                             data=json.dumps(body).encode(), headers={{"content-type": "application/json",
                             "x-api-key": "k-test"}})
try:
    reply = json.loads(urllib.request.urlopen(req, timeout=60).read())
except urllib.error.HTTPError as e:
    print(json.dumps({{"type": "result", "is_error": True, "result": f"API Error {{e.code}}", "session_id": "s1"}}))
    sys.exit(1)
print(json.dumps({{"type": "result", "is_error": False, "result": reply["content"][0]["text"], "session_id": "s1",
                  "modelUsage": {{"claude-test": {{}}}}}}))
'''


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    import uvicorn
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def messages(request):
        return JSONResponse({"id": "m", "type": "message", "role": "assistant", "model": "claude-test",
                             "content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn",
                             "usage": {"input_tokens": 1, "output_tokens": 1}})
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(Starlette(routes=[Route("/v1/messages", messages, methods=["POST"])]),
                                           host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    while not server.started:
        pass
    fake = tmp_path / "fake_claude"
    fake.write_text(FAKE_CLAUDE.format(python=sys.executable))
    fake.chmod(0o755)
    log = tmp_path / "claude.log"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    yield ["--claude", str(fake), "--recorder-upstream", f"http://127.0.0.1:{port}", "--languages", "en"], log
    server.should_exit = True
    th.join(5)


def _rd(out, arm, task, rep=1):
    return out / "runs" / arm / task / f"rep{rep}"


def test_run_records_both_arms(tmp_path, fake_env):
    args, log = fake_env
    out = tmp_path / "res"
    assert RUN.main(["--out", str(out), "--seed", "1", "--tasks", "en.reminder", *args]) == 0
    email = next(c.value for c in W.load_canaries(out / "workspace" / "canaries.json") if c.id == "client.1.email")
    direct, hermie = _rd(out, "direct", "en.reminder"), _rd(out, "hermie", "en.reminder")
    assert email in (direct / "recorded" / "001.json").read_text()
    assert email not in (hermie / "recorded" / "001.json").read_text()
    assert (direct / "status").read_text() == (hermie / "status").read_text() == "ok"
    assert (hermie / "answer.txt").read_text() == "done"
    assert list((hermie / "hermie_data" / "outbound").glob("*.json"))
    assert not (direct / "workdir" / "canaries.json").exists()
    m = json.loads((out / "manifest.json").read_text())
    assert m["sessions"] == 2 and m["seed"] == 1 and m["claude_version"].startswith("9.9.9")
    assert m["model"] == "claude-test" and m["hermie_config"]["languages"] == ["en"]


def test_failed_session_is_recorded_and_run_continues(tmp_path, fake_env, monkeypatch):
    args, log = fake_env
    monkeypatch.setenv("FAKE_CLAUDE_FAIL", "1")
    out = tmp_path / "res"
    assert RUN.main(["--out", str(out), "--tasks", "en.reminder,en.penalty", "--arms", "direct", *args]) == 0
    assert (_rd(out, "direct", "en.reminder") / "status").read_text() == "error"
    assert (_rd(out, "direct", "en.penalty") / "status").read_text() == "error"


def test_blocked_is_read_from_the_hermie_receipt(tmp_path):
    receipt = tmp_path / "receipt.jsonl"
    receipt.write_text(json.dumps({"status": 200}) + "\n" + json.dumps({"status": 422}) + "\n")
    assert RUN.classify(1, [{"is_error": True}], receipt, timed_out=False) == "blocked"
    assert RUN.classify(0, [{"is_error": False}], tmp_path / "none.jsonl", timed_out=False) == "ok"
    assert RUN.classify(None, [], tmp_path / "none.jsonl", timed_out=True) == "timeout"
    assert RUN.classify(1, [{"is_error": True}], tmp_path / "none.jsonl", timed_out=False) == "error"


def test_resume_skips_finished_runs(tmp_path, fake_env):
    args, log = fake_env
    out = tmp_path / "res"
    for _ in range(2):
        RUN.main(["--out", str(out), "--tasks", "en.penalty", "--arms", "direct", *args])
    assert len([l for l in log.read_text().splitlines() if "-p" in l]) == 1


def test_canaries_are_frozen_across_invocations(tmp_path, fake_env, capsys):
    args, log = fake_env
    out = tmp_path / "res"
    RUN.main(["--out", str(out), "--seed", "1", "--tasks", "en.penalty", "--arms", "direct", *args])
    before = (out / "workspace" / "canaries.json").read_text()
    RUN.main(["--out", str(out), "--seed", "2", "--tasks", "en.penalty", "--arms", "direct", *args])
    assert (out / "workspace" / "canaries.json").read_text() == before
    assert "frozen" in capsys.readouterr().out


def test_followups_resume_the_session(tmp_path, fake_env):
    args, log = fake_env
    out = tmp_path / "res"
    RUN.main(["--out", str(out), "--tasks", "en.owner_contact", "--arms", "direct", *args])
    calls = [l for l in log.read_text().splitlines() if "-p" in l]
    assert len(calls) == 2 and "--resume s1" in calls[1]
    assert len(json.loads((_rd(out, "direct", "en.owner_contact") / "session.json").read_text())) == 2


# ---------------------------------------------------------------- scorer

from evals.e2e import score as SC


def _mk_run(out, arm, task, answer, bodies, status="ok", rep=1, outbound=None, receipt=()):
    rd = _rd(out, arm, task, rep)
    (rd / "recorded").mkdir(parents=True)
    (rd / "workdir").mkdir()
    for i, b in enumerate(bodies, 1):
        (rd / "recorded" / f"{i:03d}.json").write_text(json.dumps(b, ensure_ascii=False))
        (rd / "recorded" / f"{i:03d}.meta.json").write_text(json.dumps({"path": "/v1/messages", "status": 200}))
    if arm == "hermie":
        (rd / "hermie_data" / "outbound").mkdir(parents=True)
        for i, b in enumerate(bodies if outbound is None else outbound, 1):
            (rd / "hermie_data" / "outbound" / f"r{i}.json").write_text(json.dumps(b))
        (rd / "hermie_data" / "receipt.jsonl").write_text("".join(json.dumps(r) + "\n" for r in receipt))
    (rd / "answer.txt").write_text(answer)
    (rd / "status").write_text(status)


@pytest.fixture
def scored_dir(tmp_path):
    out = tmp_path / "res"
    cs = {c.id: c for c in W.build(1, out / "workspace")}
    (out / "manifest.json").write_text(json.dumps({"sessions": 4, "seed": 1}))
    return out, cs


def _msg(text):
    return {"model": "m", "messages": [{"role": "user", "content": text}]}


def _score(out, judge=lambda prompt: "PASS\nok"):
    return SC.score(out, judge=judge, peers=False)


def test_claimed_and_unclaimed_leaks_are_split(scored_dir):
    out, cs = scored_dir
    email, price = cs["emp.1.email"].value, cs["en.contract.price"].value
    _mk_run(out, "direct", "en.penalty", "x", [_msg(f"{email} {price}")])
    _mk_run(out, "hermie", "en.penalty", "x", [_msg(f"{email} {price}")])
    r = _score(out)
    assert r["canaries"]["emp.1.email"] == "leaked" and r["canaries"]["en.contract.price"] == "leaked"
    assert r["claimed_leaked"] == ["emp.1.email"]
    assert r["by_kind"]["amount"]["leaked"] == 1 and r["by_kind"]["email"]["leaked"] == 1
    assert any(l["canary_id"] == "emp.1.email" and l["request"] == 1 for l in r["leaks"])


def test_unexercised_canary_counts_neither_way(scored_dir):
    out, cs = scored_dir
    _mk_run(out, "direct", "en.penalty", "x", [_msg(cs["emp.2.email"].value)])
    _mk_run(out, "hermie", "en.penalty", "x", [_msg("<EMAIL_ADDRESS_1>")])
    r = _score(out)
    assert r["canaries"]["emp.2.email"] == "protected"
    assert r["canaries"]["emp.3.email"] == "not exercised"
    assert r["by_kind"]["email"]["exercised"] == 1 and r["claimed_leaked"] == []


def test_bypass_marks_task_invalid(scored_dir):
    out, cs = scored_dir
    email = cs["emp.1.email"].value
    _mk_run(out, "direct", "en.penalty", "x", [_msg(email)])
    _mk_run(out, "hermie", "en.penalty", "x", [_msg(email)], outbound=[_msg("<EMAIL_ADDRESS_1>")])
    r = _score(out)
    assert r["invalid"] == [("hermie", "en.penalty", 1)]
    assert r["canaries"]["emp.1.email"] == "protected" and r["claimed_leaked"] == []
    assert any(l["canary_id"] == "emp.1.email" and l["invalid"] for l in r["leaks"])


def test_cause_classes(scored_dir):
    out, cs = scored_dir
    v = lambda i: cs[i].value
    _mk_run(out, "direct", "en.buyer_signatory", v("en.contract.buyer_signatory"), [_msg("a")])
    _mk_run(out, "hermie", "en.buyer_signatory", "", [_msg("a")], status="blocked")
    _mk_run(out, "direct", "en.log_user", v("log.1.email"), [_msg("a")])
    _mk_run(out, "hermie", "en.log_user", "<EMAIL_ADDRESS_3>", [_msg("a")])
    _mk_run(out, "direct", "zh.tenant_only", v("zh.lease.tenant") + v("zh.lease.tenant_phone"), [_msg("a")])
    _mk_run(out, "hermie", "zh.tenant_only", v("zh.lease.tenant") + v("zh.lease.tenant_phone") + v("zh.lease.landlord"),
            [_msg("a")])
    _mk_run(out, "direct", "en.memo", v("emp.3.name"), [_msg("a")])
    _mk_run(out, "hermie", "en.memo", "not sure", [_msg("a")],
            receipt=[{"status": 200, "withheld_items": [{"id": "x", "tool": "Read", "file": "a", "why": "w"}]}])
    r = _score(out)
    assert r["causes"] == {"en.buyer_signatory": "blocked", "en.log_user": "unrestored", "zh.tenant_only": "swapped",
                           "en.memo": "withheld"}


def test_reruns_list_disagreements_and_noise(scored_dir):
    out, cs = scored_dir
    name = cs["en.contract.buyer_signatory"].value
    _mk_run(out, "direct", "en.buyer_signatory", name, [_msg("a")])
    _mk_run(out, "hermie", "en.buyer_signatory", "no idea", [_msg("a")])
    _mk_run(out, "direct", "en.log_user", cs["log.1.email"].value, [_msg("a")])
    _mk_run(out, "hermie", "en.log_user", cs["log.1.email"].value, [_msg("a")])
    r = _score(out)
    assert r["reruns"] == ["en.buyer_signatory"] and r["causes"]["en.buyer_signatory"] == "damage"
    for rep, ans in ((2, name), (3, "no idea")):
        _mk_run(out, "direct", "en.buyer_signatory", name, [_msg("a")], rep=rep)
        _mk_run(out, "hermie", "en.buyer_signatory", ans, [_msg("a")], rep=rep)
    r = _score(out)
    assert r["reruns"] == [] and r["causes"]["en.buyer_signatory"] == "noise"


def test_judge_fail_fails_the_answer_and_is_cached(scored_dir):
    out, cs = scored_dir
    name = cs["en.contract.buyer_signatory"].value
    _mk_run(out, "direct", "en.buyer_signatory", name, [_msg("a")])
    calls = []
    r = _score(out, judge=lambda p: calls.append(p) or "FAIL\nwrong")
    assert r["quality"][("direct", "en.buyer_signatory", 1)]["passed"] is False
    _score(out, judge=lambda p: calls.append(p) or "PASS\nok")
    assert len(calls) == 1


def test_summary_gates(scored_dir):
    out, cs = scored_dir
    email = cs["emp.1.email"].value
    _mk_run(out, "direct", "en.penalty", "x", [_msg(email)])
    _mk_run(out, "hermie", "en.penalty", "x", [_msg(email)])
    assert SC.main(["--out", str(out), "--judge", "none", "--peers", "none"]) == 0
    text = (out / "summary.md").read_text()
    assert "Gate 1 (claimed kinds leak nothing): FAIL" in text
    assert "Gate 2 (no Hermie-caused wrong answer): PASS" in text
    for name in ("leaks.md", "quality.md", "reruns.txt"):
        assert (out / name).is_file()


@pytest.mark.parametrize("seed", range(1, 31))
def test_log_top_error_user_is_strictly_first(tmp_path, seed):
    from collections import Counter
    out = tmp_path / "w"
    cs = {c.id: c.value for c in W.build(seed, out)}
    errors = Counter(l.split("user=")[1].split()[0] for l in (out / "app" / "app.log").read_text().splitlines()
                     if " ERROR " in l)
    (top, n), (_, second) = errors.most_common(2)
    assert top == cs[f"log.{W.LOG_TOP_ERROR_USER}.email"] and n > second
