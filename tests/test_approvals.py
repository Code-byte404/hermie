import asyncio, io, json, os, stat, pytest
from hermie.proxy.approvals import AllowStore, SessionAllow, Approvals, TtyPrompter, NoPrompter, PendingItem


def test_allow_store_prefix_and_persistence(tmp_path):
    s = AllowStore(tmp_path)
    s.register(PendingItem(hash="ab12" + "0" * 60, kind="withhold", reason="judge", size=2300, excerpt="x"))
    assert not s.is_allowed("ab12" + "0" * 60)
    s.allow("ab12")                     # the short id typed by the user
    assert AllowStore(tmp_path).is_allowed("ab12" + "0" * 60)
    assert not AllowStore(tmp_path).is_allowed("ab13" + "0" * 60)


def test_session_allow_all(tmp_path):
    a = Approvals(AllowStore(tmp_path), SessionAllow())
    assert not a.is_allowed("ffff" + "0" * 60, "judge")
    a.session.all = True
    assert a.is_allowed("ffff" + "0" * 60, "judge")
    assert a.is_allowed("ffff" + "0" * 60, "smuggling: possible hex data: ...")


def test_session_allow_covers_judge_decisions_only(tmp_path):
    """C2(c): the session switch never releases a detector error or an image; a stored allow never releases a
    detector error either."""
    h = "ab12" + "0" * 60
    s = AllowStore(tmp_path)
    s.register(PendingItem(h, "tool result", "detector_error: RuntimeError", 10, ""))
    a = Approvals(s, SessionAllow())
    a.session.all = True
    assert not a.is_allowed(h, "detector_error: RuntimeError") and not a.is_allowed(h, "image")
    assert not s.allow(s.assign(h))                               # refused, nothing written
    assert not (tmp_path / "allowed.jsonl").exists()


def test_allow_maps_pending_id_to_full_hash_and_rereads_other_process(tmp_path):
    s = AllowStore(tmp_path)
    h = "ab12" + "0" * 60
    s.register(PendingItem(hash=h, kind="hold", reason="judge", size=5, excerpt="secret words"))
    s.allow("ab12")
    assert json.loads((tmp_path / "allowed.jsonl").read_text().splitlines()[0])["id"] == h
    # another process appends an entry; the running store must notice. A short id never acts as a prefix wildcard.
    other = "cd34" + "1" * 60
    assert not s.is_allowed(other)
    with open(tmp_path / "allowed.jsonl", "a") as f:
        f.write(json.dumps({"id": "cd34", "at": "x"}) + "\n")
        f.write(json.dumps({"id": other, "at": "x"}) + "\n")
    st = os.stat(tmp_path / "allowed.jsonl")
    os.utime(tmp_path / "allowed.jsonl", ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert s.is_allowed(other) and not s.is_allowed("cd34" + "2" * 60)


def test_allow_refuses_unknown_ids_and_never_stores_a_wildcard(tmp_path):
    """I1: an unknown id is refused; allowed.jsonl only ever holds full hashes."""
    s = AllowStore(tmp_path)
    assert s.allow("zzzz") is False and s.allow("ab12") is False
    assert not (tmp_path / "allowed.jsonl").exists()
    full = "ab12" + "0" * 60
    assert s.allow(full) is True and s.is_allowed(full) and not s.is_allowed("ab12" + "1" * 60)


def test_assign_is_the_one_id_source_and_ids_survive_a_reload(tmp_path):
    """I1: on a collision the id grows; the id the walker showed is the one pending.jsonl keeps, so another process
    (the CLI) resolves exactly that id."""
    s = AllowStore(tmp_path)
    a, b = "ab12" + "0" * 60, "ab12" + "1" * 60
    assert s.assign(a) == "ab12" and s.assign(a) == "ab12"        # idempotent
    idb = s.assign(b)
    assert idb == b[:6]
    s.register(PendingItem(b, "tool result", "judge", 1, ""))   # only b is persisted (a was a pass)
    assert s.register(PendingItem(b, "tool result", "judge", 1, "")) == idb
    other = AllowStore(tmp_path)
    assert other.pending[idb].hash == b and "ab12" not in other.pending
    assert other.allow(idb) and AllowStore(tmp_path).is_allowed(b) and not AllowStore(tmp_path).is_allowed(a)
    assert len((tmp_path / "pending.jsonl").read_text().splitlines()) == 1   # registered once


def test_pending_never_stores_excerpt_and_files_are_private(tmp_path):
    s = AllowStore(tmp_path)
    s.register(PendingItem(hash="ab12" + "0" * 60, kind="hold", reason="judge", size=5, excerpt="secret words"))
    s.allow("ab12")
    assert "secret" not in (tmp_path / "pending.jsonl").read_text()
    for name in ("pending.jsonl", "allowed.jsonl"):
        assert stat.S_IMODE(os.stat(tmp_path / name).st_mode) == 0o600
    assert AllowStore(tmp_path).pending["ab12"].hash == "ab12" + "0" * 60


def test_pending_id_grows_on_collision_and_is_capped(tmp_path):
    s = AllowStore(tmp_path)
    a, b = "ab12" + "0" * 60, "ab12" + "1" * 60
    s.register(PendingItem(a, "hold", "r", 1, ""))
    s.register(PendingItem(b, "hold", "r", 1, ""))
    assert s.pending["ab12"].hash == a and s.pending[b[:6]].hash == b
    s2 = AllowStore(tmp_path / "c")
    for i in range(1100):
        s2.register(PendingItem(f"{i:06x}" + "0" * 58, "hold", "r", 1, ""))
    assert len(AllowStore(tmp_path / "c").pending) == 500


async def test_tty_prompter_reads_choice_and_times_out():
    item = PendingItem(hash="cafe" + "0" * 60, kind="hold", reason="judge", size=40, excerpt="the layoff list")
    out = io.StringIO()
    p = TtyPrompter(timeout_s=5, stdin=io.StringIO("s\n"), stdout=out)
    assert await p.ask(item) == "send"
    assert "layoff" in out.getvalue() and "hermie allow cafe" in out.getvalue()
    p2 = TtyPrompter(timeout_s=0.2, stdin=io.StringIO(""), stdout=io.StringIO())
    assert await p2.ask(item) == "reject"
    assert await NoPrompter().ask(item) == "reject"


async def test_tty_prompter_reprompts_on_junk_and_maps_choices():
    item = PendingItem(hash="cafe" + "0" * 60, kind="hold", reason="judge", size=40, excerpt="x")
    for line, want in (("zz\n  A \n", "allow_all"), ("\nR\n", "reject")):
        p = TtyPrompter(timeout_s=5, stdin=io.StringIO(line), stdout=io.StringIO())
        assert await p.ask(item) == want
    assert not NoPrompter().available
    await asyncio.sleep(0)


async def test_concurrent_asks_are_answered_in_order():
    mk = lambda h: PendingItem(hash=h + "0" * 60, kind="hold", reason="judge", size=1, excerpt="x")
    p = TtyPrompter(timeout_s=5, stdin=io.StringIO("s\nr\n"), stdout=io.StringIO())
    first = asyncio.create_task(p.ask(mk("aaaa")))
    await asyncio.sleep(0)
    second = asyncio.create_task(p.ask(mk("bbbb")))
    assert await first == "send"
    assert await second == "reject"


async def test_timeout_with_open_pipe_rejects_and_stops_countdown():
    r, w = os.pipe()
    stdin = os.fdopen(r)
    try:
        item = PendingItem(hash="cafe" + "0" * 60, kind="hold", reason="judge", size=1, excerpt="x")
        p = TtyPrompter(timeout_s=0.3, stdin=stdin, stdout=io.StringIO())
        assert await p.ask(item) == "reject"
        assert p._ticker.done()
    finally:
        os.close(w)
        await asyncio.sleep(0.1)
        stdin.close()


def test_register_returns_assigned_id_and_sets_it_on_item(tmp_path):
    s = AllowStore(tmp_path)
    a = PendingItem("ab12" + "0" * 60, "hold", "r", 1, "")
    b = PendingItem("ab12" + "1" * 60, "hold", "r", 1, "")
    assert s.register(a) == "ab12" and a.id == "ab12"
    idb = s.register(b)
    assert len(idb) == 6 and b.id == idb
    assert set(s.pending) == {"ab12", idb}
