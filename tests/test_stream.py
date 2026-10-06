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
