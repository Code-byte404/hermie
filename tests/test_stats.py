"""Session statistics: tokens, agent run counts, current model, elapsed time."""
from hermie.events import StatsUpdated, Tainted
from hermie.session import Stats

from .conftest import FakeJudge, Script, final, text, tool


async def test_local_task_counts_executor_run_and_tokens(make_agent):
    agent = make_agent(FakeJudge(task="repetitive"))
    await agent.run("Translate")
    st = agent.session.stats.snapshot()
    assert st["runs"] == {"executor": 1} and st["active"] == [] and st["current"] is None
    assert st["local"]["requests"] >= 1 and st["local"]["input_tokens"] > 0
    assert st["cloud"]["requests"] == 0 and st["task_elapsed_s"] is None and st["session_elapsed_s"] >= 0
    assert any(isinstance(e, StatsUpdated) for e in agent.events)


async def test_plan_mode_counts_cloud_and_local_separately(make_agent):
    planner = Script([tool("delegate", step="Do step one"), tool("delegate", step="Do step two"), text("Done")],
                     name="planner")
    agent = make_agent(FakeJudge(task="planning"), planner=planner)
    await agent.run("Plan and execute two steps")
    st = agent.session.stats.snapshot()
    assert st["runs"] == {"planner": 1, "executor": 2}
    assert st["cloud"]["requests"] == 3 and st["cloud"]["input_tokens"] > 0   # the planner made 3 model requests
    assert st["local"]["requests"] >= 2


async def test_current_model_is_set_during_request(make_agent):
    seen = []

    def spy(m, info):
        seen.append(agent.session.stats.current)
        from pydantic_ai.messages import ModelResponse, TextPart
        return ModelResponse(parts=[TextPart("x")])
    cloud = Script([spy], name="deepseek-test")
    agent = make_agent(FakeJudge(task="planning", needs_ws=False), cloud=cloud)
    await agent.run("Plan")
    assert seen == ["☁ deepseek-test"] and agent.session.stats.current is None


def test_judge_usage_and_snapshot():
    s = Stats()
    changes = []
    s.on_change = lambda: changes.append(1)
    s.judge_usage(100, 3)
    s.judge_usage(120, 2)
    st = s.snapshot()
    assert st["local"] == {"requests": 2, "input_tokens": 220, "output_tokens": 5} and st["judge_requests"] == 2
    assert st["current"] is None and len(changes) == 2


async def test_compress_history_timeout_falls_back(make_agent):
    import asyncio

    from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

    from hermie.agents import HISTORY_DROPPED, compress_history

    async def slow(m, info):
        await asyncio.sleep(3)
        return ModelResponse(parts=[TextPart("summary")])
    agent = make_agent(FakeJudge(), compressor=Script([slow]))
    hist = [ModelRequest(parts=[UserPromptPart("x" * 3000)]), ModelResponse(parts=[TextPart("y" * 3000)])]
    out = await compress_history(agent.models, hist, limit=1000, timeout=0.3)
    assert len(out) == 2 and out[0].parts[0].content == HISTORY_DROPPED
    quick = make_agent(FakeJudge(), compressor=Script([text("This is a summary")]))
    out = await compress_history(quick.models, hist, limit=1000, timeout=5)
    assert "This is a summary" in out[0].parts[0].content
    assert await compress_history(quick.models, hist, limit=10000) is hist


async def test_taint_skips_judge_for_no_content_tools(make_agent):
    from hermie.privacy import CONTEXTUAL_PRIVACY_QUESTION
    judge = FakeJudge(task="repetitive")
    ex = Script([tool("write_file", path="a.txt", content="hello"), tool("list_files")])
    agent = make_agent(judge, executor=ex)
    await agent.run("Write a file")
    contextual = [s for q, s in judge.calls if q == CONTEXTUAL_PRIVACY_QUESTION]
    assert len(contextual) == 1  # only the routing stage asked once about the task text; write_file / list_files returns were not asked


def test_stats_current_elapsed():
    import time
    s = Stats()
    s.request_started("local", "m")
    time.sleep(0.05)
    assert s.snapshot()["current_elapsed_s"] >= 0.05
    s.request_finished("local", 1, 1)
    assert s.snapshot()["current_elapsed_s"] is None


async def test_contextual_taint_check_does_not_block_tool_return(make_agent, settings):
    """The judge's contextual check runs in the background: the second tool call should happen before the first one's
    judge call returns; everything is settled before the task ends."""
    import threading
    from hermie.privacy import CONTEXTUAL_PRIVACY_QUESTION

    release = threading.Event()
    observed = {"second_tool_before_judge": False}

    class SlowJudge(FakeJudge):
        def noul(self, state, statement):
            if statement == CONTEXTUAL_PRIVACY_QUESTION and "FILE_BODY" in state:
                release.wait(timeout=5)
                return 1.0   # contextually sensitive -> must end up marked as tainted
            return super().noul(state, statement)

    settings.workspace.mkdir(parents=True, exist_ok=True)
    (settings.workspace / "a.txt").write_text("FILE_BODY internal material")

    def second(m, info):
        observed["second_tool_before_judge"] = not release.is_set()
        release.set()
        return tool("list_files")(m, info)

    ex = Script([tool("read_file", path="a.txt"), second], final=final())
    agent = make_agent(SlowJudge(task="repetitive"), executor=ex)
    r = await agent.run("Look at a.txt")
    assert observed["second_tool_before_judge"]
    assert r.tainted and any(isinstance(e, Tainted) for e in agent.events)
