import asyncio, io, json, os, stat, pytest
from hermie.proxy.approvals import AllowStore, SessionAllow, Approvals, TtyPrompter, NoPrompter, DialogPrompter, PendingItem


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
    assert await p2.ask(item) == "unavailable"          # stdin closed: nobody can answer
    assert await NoPrompter().ask(item) == "unavailable"


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


async def test_timeout_with_open_pipe_times_out_and_stops_countdown():
    r, w = os.pipe()
    stdin = os.fdopen(r)
    try:
        item = PendingItem(hash="cafe" + "0" * 60, kind="hold", reason="judge", size=1, excerpt="x")
        p = TtyPrompter(timeout_s=0.3, stdin=stdin, stdout=io.StringIO())
        assert await p.ask(item) == "timeout"
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


def _dialog(answers):
    """A fake osascript runner: returns the next scripted button, records each (title, text, buttons)."""
    seen = []

    async def run(title, text, buttons, timeout_s):
        seen.append((title, text, buttons))
        return answers.pop(0)
    return run, seen


async def test_dialog_prompter_asks_once():
    item = PendingItem(hash="cafe" + "0" * 60, kind="message", reason="judge", size=40, excerpt="the layoff list", id="cafe")
    run, seen = _dialog(["Send this one"])
    assert await DialogPrompter(5, runner=run).ask(item) == "send"
    assert len(seen) == 1 and "layoff" in seen[0][1] and "cafe" in seen[0][1]
    for button, want in (("Don't send", "reject"), ("Send all this session", "allow_all"), (None, "timeout")):
        run, seen = _dialog([button])
        assert await DialogPrompter(5, runner=run).ask(item) == want and len(seen) == 1

    async def broken(*a):
        raise OSError("no osascript")
    assert await DialogPrompter(5, runner=broken).ask(item) == "unavailable"


async def test_dialog_prompter_reuses_the_answer_for_a_request_that_wraps_the_same_text():
    text = "Our patient was diagnosed with stage 2 breast cancer last month and her family is struggling"
    main = PendingItem(hash="aaaa" + "0" * 60, kind="message", reason="judge", size=90, excerpt=text)
    title = PendingItem(hash="bbbb" + "0" * 60, kind="message", reason="judge", size=300,
                        excerpt=f"<session> {text} </session> Write the title in the predominant language")
    other = PendingItem(hash="cccc" + "0" * 60, kind="message", reason="judge", size=40, excerpt="the layoff list is final")
    run, seen = _dialog(["Send this one", "Don't send"])
    p = DialogPrompter(5, runner=run)
    assert await p.ask(main) == "send"
    assert await p.ask(title) == "send" and len(seen) == 1      # same text, no second dialog
    assert await p.ask(other) == "reject" and len(seen) == 2    # unrelated text asks again


async def test_dialog_prompter_asks_again_for_a_different_message_with_the_same_opening():
    opening = "Please draft a reply to this customer complaint about the delayed refund for order "
    first = PendingItem(hash="aaaa" + "0" * 60, kind="message", reason="judge", size=120, excerpt=opening + "A-1001, Jane at 555-0100")
    second = PendingItem(hash="bbbb" + "0" * 60, kind="message", reason="judge", size=120, excerpt=opening + "B-2002, she has cancer")
    run, seen = _dialog(["Send this one", "Don't send"])
    p = DialogPrompter(5, runner=run)
    assert await p.ask(first) == "send"
    assert await p.ask(second) == "reject" and len(seen) == 2


async def test_dialog_prompter_does_not_reuse_an_answer_for_a_cut_excerpt():
    head = "x" * 50 + " the quarterly numbers for the board, " * 8
    first = PendingItem(hash="aaaa" + "0" * 60, kind="message", reason="judge", size=900, excerpt=(head + "part one")[:300])
    second = PendingItem(hash="bbbb" + "0" * 60, kind="message", reason="judge", size=900, excerpt=(head + "part two")[:300])
    assert first.excerpt == second.excerpt
    run, seen = _dialog(["Send this one", "Don't send"])
    p = DialogPrompter(5, runner=run)
    assert await p.ask(first) == "send"
    assert await p.ask(second) == "reject" and len(seen) == 2
    assert await p.ask(first) == "send" and len(seen) == 2       # the same item: no second dialog


async def test_mod_prompter_waits_for_the_menu_answer_and_clears_the_excerpt(tmp_path):
    from hermie.proxy.approvals import ModPrompter, read_asks, touch_ui, write_answer
    item = PendingItem(hash="cafe" + "0" * 60, kind="message", reason="judge", size=40, excerpt="the layoff list", id="cafe")
    touch_ui(tmp_path)
    p = ModPrompter(tmp_path, 5, poll_s=0.01)
    task = asyncio.create_task(p.ask(item))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if read_asks(tmp_path):
            break
    asks = read_asks(tmp_path)
    assert [{k: v for k, v in a.items() if k != "deadline"} for a in asks] == [
        {"id": "cafe", "kind": "message", "reason": "judge", "size": 40, "excerpt": "the layoff list", "placeholders": []}]
    assert write_answer(tmp_path, "cafe", "session") and not write_answer(tmp_path, "nope", "send")
    assert await task == "allow_all"
    assert read_asks(tmp_path) == []


async def test_mod_prompter_falls_back_when_no_menu_polls(tmp_path):
    from hermie.proxy.approvals import ModPrompter
    item = PendingItem(hash="cafe" + "0" * 60, kind="message", reason="judge", size=40, excerpt="x", id="cafe")
    run, seen = _dialog(["Send this one"])
    p = ModPrompter(tmp_path, 5, fallback=DialogPrompter(5, runner=run))
    assert await p.ask(item) == "send" and len(seen) == 1
    assert await ModPrompter(tmp_path, 5).ask(item) == "unavailable"


async def test_mod_prompter_times_out(tmp_path):
    from hermie.proxy.approvals import ModPrompter, touch_ui
    item = PendingItem(hash="cafe" + "0" * 60, kind="message", reason="judge", size=40, excerpt="x", id="cafe")
    touch_ui(tmp_path)
    assert await ModPrompter(tmp_path, 0.1, poll_s=0.01).ask(item) == "timeout"


async def test_mod_prompter_publishes_local_flag_and_returns_local(tmp_path):
    from hermie.proxy.approvals import ModPrompter, read_asks, touch_ui, write_answer
    item = PendingItem(hash="cafe" + "0" * 60, kind="message", reason="judge", size=40, excerpt="the layoff list",
                       id="cafe", local_ok=True)
    touch_ui(tmp_path)
    p = ModPrompter(tmp_path, 5, poll_s=0.01)
    task = asyncio.create_task(p.ask(item))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if read_asks(tmp_path):
            break
    [row] = read_asks(tmp_path)
    assert row["local"] is True and row["excerpt"] == "the layoff list"
    assert write_answer(tmp_path, "cafe", "local")
    assert await task == "local"
    assert "layoff" not in (tmp_path / "asks.json").read_text()


async def test_local_answer_is_refused_when_the_ask_has_no_local_flag(tmp_path):
    from hermie.proxy.approvals import ModPrompter, read_asks, touch_ui, write_answer
    item = PendingItem(hash="cafe" + "0" * 60, kind="message", reason="judge", size=40, excerpt="x", id="cafe")
    touch_ui(tmp_path)
    task = asyncio.create_task(ModPrompter(tmp_path, 0.3, poll_s=0.01).ask(item))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if read_asks(tmp_path):
            break
    assert not write_answer(tmp_path, "cafe", "local")
    assert await task == "timeout"


async def _wait_ask(tmp_path):
    from hermie.proxy.approvals import read_asks
    for _ in range(100):
        await asyncio.sleep(0.01)
        if read_asks(tmp_path):
            return read_asks(tmp_path)
    return []


async def test_review_publishes_the_rewrite_and_clears_it_after_the_answer(tmp_path):
    from hermie.proxy.approvals import ModPrompter, read_asks, touch_ui, write_answer
    item = PendingItem(hash="cafe" + "0" * 60, kind="message", reason="judge", size=40, excerpt="x", id="cafe", local_ok=True)
    touch_ui(tmp_path)
    task = asyncio.create_task(ModPrompter(tmp_path, 5, poll_s=0.01).review(item, "a staffing plan"))
    assert [{k: v for k, v in a.items() if k != "deadline"} for a in await _wait_ask(tmp_path)] == [
        {"id": "cafe", "kind": "rewrite", "reason": "judge", "size": 15, "preview": "a staffing plan"}]
    assert write_answer(tmp_path, "cafe", "again")
    assert await task == "again"
    assert read_asks(tmp_path) == [] and "staffing" not in (tmp_path / "asks.json").read_text()


@pytest.mark.parametrize("answer,verdict", [("send", "send"), ("again", "again"), ("reject", "discard")])
async def test_review_answers(tmp_path, answer, verdict):
    from hermie.proxy.approvals import ModPrompter, touch_ui, write_answer
    item = PendingItem(hash="cafe" + "0" * 60, kind="message", reason="judge", size=40, excerpt="x", id="cafe")
    touch_ui(tmp_path)
    task = asyncio.create_task(ModPrompter(tmp_path, 5, poll_s=0.01).review(item, "text"))
    await _wait_ask(tmp_path)
    assert write_answer(tmp_path, "cafe", answer)
    assert await task == verdict


async def test_review_refuses_session_and_local_answers_and_times_out_to_discard(tmp_path):
    from hermie.proxy.approvals import ModPrompter, touch_ui, write_answer
    item = PendingItem(hash="cafe" + "0" * 60, kind="message", reason="judge", size=40, excerpt="x", id="cafe", local_ok=True)
    touch_ui(tmp_path)
    task = asyncio.create_task(ModPrompter(tmp_path, 0.3, poll_s=0.01).review(item, "text"))
    await _wait_ask(tmp_path)
    assert not write_answer(tmp_path, "cafe", "session") and not write_answer(tmp_path, "cafe", "local")
    assert await task == "discard"


async def test_review_without_a_menu_is_a_discard_and_again_needs_a_shown_rewrite(tmp_path):
    from hermie.proxy.approvals import ModPrompter, touch_ui, write_answer
    item = PendingItem(hash="cafe" + "0" * 60, kind="message", reason="judge", size=40, excerpt="x", id="cafe")
    assert await ModPrompter(tmp_path, 0.1, poll_s=0.01).review(item, "text") == "discard"
    touch_ui(tmp_path)
    task = asyncio.create_task(ModPrompter(tmp_path, 0.3, poll_s=0.01).ask(item))
    await _wait_ask(tmp_path)
    assert not write_answer(tmp_path, "cafe", "again")
    assert await task == "timeout"


def test_approvals_rewrites_start_empty(tmp_path):
    from hermie.proxy.approvals import AllowStore, Approvals, SessionAllow
    assert Approvals(AllowStore(tmp_path), SessionAllow()).rewrites == {}


def test_an_id_shown_once_never_names_another_item(tmp_path, monkeypatch):
    import hermie.proxy.approvals as ap
    monkeypatch.setattr(ap, "KEEP", 2)
    s = AllowStore(tmp_path)
    x, y = "abcd" + "0" * 60, "abcd" + "1" * 60
    assert s.register(PendingItem(x, "hold", "judge", 1, "")) == "abcd"
    s.assign("1111" + "0" * 60)
    s.assign("2222" + "0" * 60)                       # x falls out of the in-memory table
    assert "abcd" not in s.pending
    idy = s.register(PendingItem(y, "hold", "judge", 1, ""))
    assert idy != "abcd"
    cli = AllowStore(tmp_path)
    assert cli.pending["abcd"].hash == x and cli.pending[idy].hash == y


def test_the_newest_row_wins_an_id_in_an_old_pending_file(tmp_path):
    x, y = "abcd" + "0" * 60, "abcd" + "1" * 60
    with open(tmp_path / "pending.jsonl", "w") as f:
        for h in (x, y):
            f.write(json.dumps({"hash": h, "id": "abcd", "kind": "hold", "reason": "judge", "size": 1, "at": "t"}) + "\n")
    assert AllowStore(tmp_path).pending["abcd"].hash == y


async def test_timeouts_and_missing_prompters_are_not_a_choice(tmp_path):
    from hermie.proxy.approvals import ModPrompter, touch_ui
    item = PendingItem(hash="cafe" + "0" * 60, kind="message", reason="judge", size=40, excerpt="x", id="cafe")
    assert await NoPrompter().ask(item) == "unavailable"
    assert await ModPrompter(tmp_path, 5).ask(item) == "unavailable"            # no menu, no fallback
    touch_ui(tmp_path)
    assert await ModPrompter(tmp_path, 0.1, poll_s=0.01).ask(item) == "timeout"
    run, _ = _dialog([None])
    assert await DialogPrompter(5, runner=run).ask(item) == "timeout"


async def test_ask_row_carries_excerpt_placeholders_and_deadline(tmp_path):
    import time as _t
    from hermie.proxy.approvals import ModPrompter, read_asks, touch_ui, write_answer
    item = PendingItem(hash="cafe" + "0" * 60, kind="message", reason="judge", size=40,
                       excerpt="lay off <PERSON_1> and <PERSON_2>", id="cafe")
    touch_ui(tmp_path)
    task = asyncio.create_task(ModPrompter(tmp_path, 30, poll_s=0.01).ask(item))
    while not read_asks(tmp_path):
        await asyncio.sleep(0.01)
    [row] = read_asks(tmp_path)
    assert row["excerpt"] == "lay off <PERSON_1> and <PERSON_2>" and row["placeholders"] == ["<PERSON_1>", "<PERSON_2>"]
    assert _t.time() + 25 < row["deadline"] <= _t.time() + 30
    write_answer(tmp_path, "cafe", "reject")
    assert await task == "reject" and read_asks(tmp_path) == []


async def test_dialog_defaults_to_dont_send_and_shows_consequences():
    item = PendingItem(hash="cafe" + "0" * 60, kind="message", reason="judge", size=40, excerpt="hi <PERSON_1>",
                       id="cafe", mapping={"<PERSON_1>": "Alice Chen"})
    run, seen = _dialog(["Send this one"])
    assert await DialogPrompter(5, runner=run).ask(item) == "send"
    title, text, buttons = seen[0]
    assert buttons[-1] == "Don't send" and "Send all this session" in buttons
    assert "Why: a local model judged it private" in text and "Alice Chen -> <PERSON_1>" in text


async def test_terminal_prompt_uses_the_same_words():
    out = io.StringIO()
    p = TtyPrompter(5, stdin=io.StringIO("n\n"), stdout=out)
    item = PendingItem(hash="cafe" + "0" * 60, kind="message", reason="judge", size=40, excerpt="hi", id="cafe")
    assert await p.ask(item) == "reject"
    text = out.getvalue()
    assert "[n] Don't send" in text and "[s] Send this one" in text and "kind:" not in text
