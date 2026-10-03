"""ConnectorGuard: the business flag, data room, state, logs and errors around every connector call."""
import json
import os
import time

from hermie.connectors.base import ConnectorResult, ConnectorTool
from hermie.connectors.guard import ConnectorGuard, StateStore, run_connector_tool
from hermie.events import ChoiceRequest, EventBus, Notice
from hermie.session import TaskState

from .conftest import FakeConnector


def _st(agent, tmp_path):
    st = TaskState(agent.session, "how many downloads")
    st.data_room = tmp_path / "room"
    st.data_room.mkdir()
    return st


async def test_flag_set_before_the_call_even_if_it_raises(make_agent, tmp_path):
    agent = make_agent()
    st = _st(agent, tmp_path)
    seen = {}

    async def boom(args, ctx):
        seen["business"] = st.business
        raise RuntimeError("asc exploded")
    t = ConnectorTool("boom", "x", {"type": "object"}, boom)
    out = await run_connector_tool(st, FakeConnector(), t, {})
    assert seen["business"] is True and st.business and agent.session.business
    assert "boom failed: RuntimeError: asc exploded" in out
    assert any(isinstance(e, Notice) and "Business data" in e.text for e in agent.events)


async def test_result_rendered_with_saved_paths_and_logged_without_data(make_agent, tmp_path, settings):
    agent = make_agent()
    st = _st(agent, tmp_path)
    conn = FakeConnector()
    out = await run_connector_tool(st, conn, conn.tools()[0], {"query": "q"})
    assert FakeConnector.FIGURE in out and "Full output saved (read-only" in out and "001-fake.json" in out
    log = settings.connector_log_path.read_text()
    rec = json.loads(log.splitlines()[-1])
    assert rec["connector"] == "fake" and rec["tool"] == "fake_lookup" and rec["label"] == "lookup" and rec["ok"]
    assert FakeConnector.FIGURE not in log and "q" not in json.dumps(rec.get("args", ""))


async def test_room_paths_are_numbered_and_sanitized(make_agent, tmp_path):
    agent = make_agent()
    st = _st(agent, tmp_path)
    g = ConnectorGuard(st, "asc")
    a, b = g.room_path("x.json"), g.room_path("../evil/y.csv")
    assert a.name == "001-x.json"
    assert b.parent == st.data_room and b.name.startswith("002-") and "/" not in b.name


async def test_state_persisted_and_merged(make_agent, tmp_path, settings):
    agent = make_agent()
    st = _st(agent, tmp_path)

    async def remember(args, ctx):
        ctx.state()["vendor"] = "123"
        ctx.session_state()["app"] = "9"
        return ConnectorResult("ok")
    StateStore(settings.connector_dir).save("fake", {"catalog": {"apps": []}})
    await run_connector_tool(st, FakeConnector(), ConnectorTool("r", "x", {"type": "object"}, remember), {})
    data = StateStore(settings.connector_dir).load("fake")
    assert data == {"catalog": {"apps": []}, "vendor": "123"}
    assert agent.session.connector_session["fake"] == {"app": "9"}


async def test_choose_and_ask_go_through_the_bus(make_agent, tmp_path):
    agent = make_agent()
    st = _st(agent, tmp_path)
    asked = []

    async def chooser(req: ChoiceRequest):
        asked.append(req)
        return req.options[1]

    async def provider(req):
        return "42"
    agent.bus.chooser, agent.bus.input_provider = chooser, provider
    g = ConnectorGuard(st, "asc")
    assert await g.choose("Which app?", ["A", "B"]) == "B" and asked[0].prompt == "Which app?"
    assert await g.ask("vendor?") == "42"
    agent.bus.chooser = agent.bus.input_provider = None
    assert await g.choose("Which app?", ["A"]) is None and await g.ask("x") is None


def test_eventbus_request_choice_counts_user_wait():
    import asyncio
    bus = EventBus()

    async def chooser(req):
        await asyncio.sleep(0.05)
        return "A"
    bus.chooser = chooser
    assert asyncio.run(bus.request_choice(ChoiceRequest("p", ["A"]))) == "A"
    assert bus.user_wait_s() >= 0.04


def test_exposed_includes_business(make_agent):
    st = TaskState(make_agent().session, "x")
    assert not st.exposed
    st.business = True
    assert st.exposed
