import json, pytest
from pathlib import Path
from hermie.proxy.stream import PlaceholderBuffer, restore_event, relay_sse, restore_json, StreamStats

MAP = {"<PHONE_NUMBER_1>": "555-010-0199", "<SECRET_1>": "sk-test-abc123"}
def R(s):
    from hermie.gate.redact import restore
    return restore(s, MAP)

FIX = Path(__file__).parent / "fixtures" / "streams"

@pytest.mark.parametrize("cut", range(1, len("<PHONE_NUMBER_1>")))
def test_buffer_reassembles_any_split(cut):
    ph = "<PHONE_NUMBER_1>"; b = PlaceholderBuffer(R)
    out = b.feed("call " + ph[:cut]) + b.feed(ph[cut:] + " now") + b.flush()
    assert out == "call 555-010-0199 now"

def test_buffer_releases_non_placeholders_quickly():
    b = PlaceholderBuffer(R)
    assert b.feed("a <b> tag and 1 < 2") == "a <b> tag and 1 < 2"
    assert b.feed("<NOT_A_PLACEHOLDER_BECAUSE_IT_IS_TOO_LONG_XXXXXXXX") .startswith("<NOT_A")

def test_buffer_flush_at_eof_keeps_raw_tail_and_counts():
    b = PlaceholderBuffer(R); b.feed("x <PHONE_NUM")
    assert b.flush() == "<PHONE_NUM"          # Review Focus 5: nothing lost

def test_restore_json_counts_unknown():
    obj, n = restore_json({"a": ["<PHONE_NUMBER_1>", {"b": "<FOO_9>"}], "n": 1}, R)
    assert obj == {"a": ["555-010-0199", {"b": "<FOO_9>"}], "n": 1} and n == 1

@pytest.mark.parametrize("fmt", ["anthropic", "openai_chat", "openai_responses", "gemini"])
async def test_relay_restores_text_and_tool_args(fmt):
    raw = (FIX / f"{fmt}.sse").read_bytes()
    async def chunks():
        for i in range(0, len(raw), 37): yield raw[i:i + 37]      # arbitrary chunking across event boundaries
    stats = StreamStats()
    out = b"".join([c async for c in relay_sse(chunks(), R, stats)]).decode()
    assert "555-010-0199 works" in out and "sk-test-abc123" in out
    assert "<PHONE_NUMBER_1>" not in out and "<SECRET_1>" not in out and "a <b> tag" in out
    assert out.count("event:") == raw.decode().count("event:")      # no event lost or duplicated
    assert stats.unrestored == 0


async def test_relay_eof_flush_lands_in_last_event_and_done_passes():
    raw = b'data: {"delta":{"text":"x <PHONE_NUM"}}\n\ndata: {"delta":{"text":"BER_1>"}}\n\n'
    async def chunks():
        yield raw
    stats = StreamStats()
    out = b"".join([c async for c in relay_sse(chunks(), R, stats)]).decode()
    assert out == 'data: {"delta":{"text":"x "}}\n\ndata: {"delta":{"text":"555-010-0199"}}\n\n'
    async def done():
        yield b"data: [DONE]\n\n"
    assert b"".join([c async for c in relay_sse(done(), R, StreamStats())]) == b"data: [DONE]\n\n"


async def _relay(raw, stats=None):
    async def chunks():
        for i in range(0, len(raw), 5): yield raw[i:i + 5]
    return b"".join([c async for c in relay_sse(chunks(), R, stats or StreamStats())]).decode()


def _ev(**d):
    return "data: " + json.dumps(d, separators=(",", ":")) + "\n\n"


async def test_held_tail_does_not_spill_into_next_block():
    raw = ("event: content_block_delta\n" + _ev(type="content_block_delta", delta={"type": "text_delta", "text": "a <"})
           + "event: content_block_stop\n" + _ev(type="content_block_stop", index=0)
           + "event: content_block_delta\n" + _ev(type="content_block_delta", delta={"type": "input_json_delta", "partial_json": '{"k":1}'}))
    out = await _relay(raw.encode())
    datas = [json.loads(l[6:]) for l in out.splitlines() if l.startswith("data: ")]
    assert datas[0]["delta"]["text"] == "a <"
    assert datas[2]["delta"]["partial_json"] == '{"k":1}'
    assert [d["type"] for d in datas] == ["content_block_delta", "content_block_stop", "content_block_delta"]


async def test_responses_done_events_are_not_corrupted_by_held_tails():
    full = "call <PHONE_NUMBER_1> works"
    raw = ("event: response.output_text.delta\n" + _ev(type="response.output_text.delta", delta="call <PHONE_NUMBER_1> works <PHONE_NUM")
           + "event: response.output_text.done\n" + _ev(type="response.output_text.done", text=full + " <")
           + "event: response.completed\n" + _ev(type="response.completed", response={"status": "completed"}))
    out = await _relay(raw.encode())
    datas = [json.loads(l[6:]) for l in out.splitlines() if l.startswith("data: ")]
    assert datas[0]["delta"] == "call 555-010-0199 works <PHONE_NUM"      # incomplete tail flushed raw into its own event
    assert datas[1]["text"] == "call 555-010-0199 works <"                # done text unchanged, own tail flushed back
    assert datas[2]["response"] == {"status": "completed"}


async def test_ping_inside_a_split_placeholder_does_not_flush():
    raw = ("event: content_block_delta\n" + _ev(type="content_block_delta", delta={"text": "x <PHONE_NU"})
           + "event: ping\n" + _ev(type="ping")
           + "event: content_block_delta\n" + _ev(type="content_block_delta", delta={"text": "MBER_1> y"}))
    out = await _relay(raw.encode())
    assert "555-010-0199 y" in out and "<PHONE" not in out and out.count("event:") == 3


async def test_crlf_framing_relays_like_lf():
    raw = (FIX / "anthropic.sse").read_bytes()
    lf = await _relay(raw)
    crlf = await _relay(raw.replace(b"\n", b"\r\n"))
    assert crlf == lf.replace("\n", "\r\n")


async def test_multiline_data_event_keeps_both_lines_of_content():
    raw = b'event: x\ndata: {"a":\ndata: "<PHONE_NUMBER_1>"}\n\n'
    out = await _relay(raw)
    assert out == 'event: x\ndata: {"a":"555-010-0199"}\n\n'
    out2 = await _relay(b"data: line <PHONE_NUMBER_1>\ndata: two\n\n")
    assert out2 == "data: line 555-010-0199\ndata: two\n\n"


async def test_unterminated_final_event_is_restored_and_terminated():
    out = await _relay(b'data: {"text":"hi"}\n\ndata: {"text":"<PHONE_NUMBER_1>"}')
    assert out == 'data: {"text":"hi"}\n\ndata: {"text":"555-010-0199"}\n\n'


async def test_synthetic_flush_event_when_tail_cannot_be_appended(monkeypatch):
    # With the holder rule the tail always has an event to land in; force the last resort by refusing the append.
    from hermie.proxy import stream
    monkeypatch.setattr(stream._Event, "append_text", lambda self, text: False)
    stats = StreamStats()
    out = await _relay(_ev(text="x <PHONE_NUMBER_1>").encode() + _ev(text="y <PHONE_NUMBER_1>").encode()[:0]
                       + b'data: {"text":"z <PHONE_NUMBER_1"}\n\n', stats)
    assert stats.synthetic == 1
    assert out.endswith('data: {"hermie_flush":"<PHONE_NUMBER_1"}\n\n')


async def test_several_buffered_leaves_in_one_event_keep_their_own_tails():
    """I4: a message text ending in a held tail must not prefix the function call arguments of the same event."""
    completed = {"type": "response.completed", "response": {"output": [
        {"type": "message", "content": [{"type": "output_text", "text": "see <PHONE_NUMBER_1> or <B"}]},
        {"type": "function_call", "name": "run", "arguments": '{"x":1}'},
        {"type": "function_call", "name": "dial", "arguments": '{"to":"<PHONE_NUMBER_1>"}'}]}}
    raw = "event: response.completed\n" + _ev(**completed)
    out = await _relay(raw.encode())
    data = json.loads(out.splitlines()[1][6:])["response"]["output"]
    assert data[0]["content"][0]["text"] == "see 555-010-0199 or <B"
    assert data[1]["arguments"] == '{"x":1}'
    assert data[2]["arguments"] == '{"to":"555-010-0199"}'


async def test_tail_from_an_earlier_event_returns_there_when_the_next_leaf_differs():
    raw = (_ev(type="d", delta={"text": "x <PHONE_NUM"})
           + _ev(type="d", item={"text": "other"}, delta={"text": "BER_1> y"}))
    out = await _relay(raw.encode())
    datas = [json.loads(l[6:]) for l in out.splitlines() if l.startswith("data: ")]
    assert datas[0]["delta"]["text"] == "x <PHONE_NUM" and datas[1]["item"]["text"] == "other"


def test_restore_json_restores_keys():
    obj, n = restore_json({"args": {"<PHONE_NUMBER_1>": {"<SECRET_1>": "<PHONE_NUMBER_1>"}}, "n": 4096}, R)
    assert obj == {"args": {"555-010-0199": {"sk-test-abc123": "555-010-0199"}}, "n": 4096} and n == 0


async def test_stream_restores_keys():
    out = await _relay(_ev(functionCall={"name": "f", "args": {"<PHONE_NUMBER_1>": "to"}}).encode())
    assert json.loads(out[6:])["functionCall"]["args"] == {"555-010-0199": "to"}


# the phone in the text and the key in the tool args; the Responses API repeats the final text in its `done` event
@pytest.mark.parametrize("fmt,n", [("anthropic", 2), ("openai_chat", 2), ("openai_responses", 3), ("gemini", 2)])
async def test_relay_counts_restored_placeholders(fmt, n):
    raw = (FIX / f"{fmt}.sse").read_bytes()
    async def chunks():
        for i in range(0, len(raw), 37): yield raw[i:i + 37]
    stats = StreamStats()
    b"".join([c async for c in relay_sse(chunks(), R, stats)])
    assert stats.restored == n and stats.unrestored == 0


async def test_relay_restored_excludes_unknown_placeholders():
    stats = StreamStats()
    await _relay(b'data: {"delta":{"text":"<PHONE_NUMBER_1> and <FOO_9> and <SEC"}}\n\ndata: {"delta":{"text":"RET_1>"}}\n\n', stats)
    assert stats.restored == 2 and stats.unrestored == 1


def test_restore_json_counts_restored():
    stats = StreamStats()
    obj, n = restore_json({"a": ["<PHONE_NUMBER_1>", {"b": "<FOO_9>", "<SECRET_1>": "<SECRET_1>"}]}, R, stats)
    assert n == 1 and stats.unrestored == 1 and stats.restored == 3
