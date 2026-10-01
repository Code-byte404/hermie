"""Plan mode design phase: questions, a structured plan, approval, PLAN.md, and the privacy of answers."""
import asyncio

import pytest

from hermie.events import ClarifyAnswer, ClarifyRequest, EventBus, PlanDecision, PlanReviewRequest, QuestionView


async def test_bus_without_providers_never_blocks():
    bus = EventBus()
    assert await bus.request_clarification(ClarifyRequest(1, [QuestionView("q", ["a", "b"])])) is None
    d = await bus.request_plan_review(PlanReviewRequest({}, "", 1, {}))
    assert d.action == "approve"


async def test_bus_wait_time_is_counted_as_user_wait():
    bus = EventBus()

    async def slow(req):
        await asyncio.sleep(0.05)
        return [ClarifyAnswer(option=0)]
    bus.clarifier = slow
    assert (await bus.request_clarification(ClarifyRequest(1, [QuestionView("q", ["a", "b"])])))[0].option == 0
    assert bus.user_wait_s() >= 0.04


from hermie.config import RunMode
from hermie.events import Notice, PlanProposed, PlanUpdated
from tests.conftest import FakeJudge, Script, final, review, text, tool

PHONE = "13812345678"
PHONE2 = "13987654321"

STEPS = [{"title": "Skeleton app", "details": "Create a SwiftUI app that builds", "files": ["App.swift"],
          "acceptance": ["xcodebuild succeeds"]},
         {"title": "Feed list", "details": "Parse the RSS feed and list headlines", "files": ["Feed.swift"],
          "acceptance": ["list shows headlines"], "depends_on": [1]}]


def submit(**over):
    args = {"goal": "A news reader", "architecture": "SwiftUI + RSS", "steps": STEPS,
            "assumptions": ["English UI"], **over}
    return tool("submit_plan", **args)


def ask(*qs):
    return tool("ask_user", questions=[{"question": q, "options": ["iOS", "Web"], "why": "platform"} for q in qs])


async def test_submit_plan_in_execution_phase_without_design(make_agent):
    """PLAN_DESIGN=false (the test default): submit_plan is a plain tool in the plan run and is auto-approved."""
    planner = Script([submit(), tool("delegate", step="Build the skeleton", plan_step=1), text("Done")], name="planner")
    agent = make_agent(FakeJudge(task="planning"), planner=planner)
    r = await agent.run("Build a news app")
    assert r.route == "plan"
    ups = [e for e in agent.events if isinstance(e, PlanUpdated)]
    assert ups[-1].outline == ["Skeleton app", "Feed list"]
    assert [e.approved_by for e in agent.events if isinstance(e, PlanProposed)] == ["auto"]
    assert "Remaining delegations: 7" in planner.sent_text()   # max(8, 2 x 2) - 1


async def test_ask_user_sends_cloud_option_text_not_restored_text(make_agent):
    """The chosen option goes back as the planner wrote it (placeholder kept), never restored."""
    planner = Script([tool("ask_user", questions=[{"question": "Contact <CN_MOBILE_1> by?",
                                                   "options": ["SMS to <CN_MOBILE_1>", "Email"]}]),
                      submit(), text("Done")], name="planner")
    agent = make_agent(FakeJudge(task="planning"), planner=planner)
    seen = []

    async def clarifier(req):
        seen.append(req)
        return [ClarifyAnswer(option=0)]
    agent.bus.clarifier = clarifier
    await agent.run(f"Build a reminder tool for customer {PHONE}")
    assert seen[0].questions[0].options[0] == f"SMS to {PHONE}"      # the user sees the restored text
    sent = planner.sent_text()
    assert "Q1: SMS to <CN_MOBILE_1>" in sent and PHONE not in sent


async def test_free_text_answer_with_second_phone_keeps_both_mappings(make_agent):
    planner = Script([ask("Who gets the alerts?"),
                      tool("delegate", step="Send a test alert to <CN_MOBILE_2> and <CN_MOBILE_1>"),
                      text("Done")], name="planner")
    ex = Script(final=final())
    agent = make_agent(FakeJudge(task="planning"), planner=planner, executor=ex)

    async def clarifier(req):
        return [ClarifyAnswer(text=f"Only {PHONE2}")]
    agent.bus.clarifier = clarifier
    await agent.run(f"Build an alert tool; my phone is {PHONE}")
    sent = planner.sent_text()
    assert PHONE not in sent and PHONE2 not in sent and "<CN_MOBILE_2>" in sent
    prompt = ex.sent_text()
    assert PHONE in prompt and PHONE2 in prompt                       # both restored for the executor
    notices = [e.text for e in agent.events if isinstance(e, Notice)]
    assert any("Your answer to Q1 was sent as" in n for n in notices)


async def test_withheld_answer_sends_template_and_marks_task_sensitive(make_agent):
    planner = Script([ask("What is the project about?"), text("Done")], name="planner")
    comp = Script([text("still about the merger with Acme")], name="comp")
    agent = make_agent(FakeJudge(task="planning", secrets=("merger",)), planner=planner, compressor=comp)

    async def clarifier(req):
        return [ClarifyAnswer(text="the secret merger with Acme")]
    agent.bus.clarifier = clarifier
    r = await agent.run("Write a project tracker")
    sent = planner.sent_text()
    assert "merger" not in sent and "Q1: answer withheld (private)" in sent


async def test_short_or_failed_clarifier_answers_are_skipped(make_agent):
    planner = Script([ask("Platform?", "Data source?"), ask("Offline?"), text("Done")], name="planner")
    agent = make_agent(FakeJudge(task="planning"), planner=planner)
    calls = []

    async def clarifier(req):
        calls.append(req)
        if len(calls) == 1:
            return [ClarifyAnswer(option=1)]          # one answer for two questions
        raise RuntimeError("dialog crashed")
    agent.bus.clarifier = clarifier
    r = await agent.run("Build a news app")
    sent = planner.sent_text()
    assert "Q1: Web" in sent and "Q2: user skipped" in sent
    assert "Q1: user skipped" in sent                  # second round: the dialog raised
    assert r.route == "plan"


async def test_question_round_limit_hides_ask_user(make_agent):
    planner = Script([ask("a?"), ask("b?"), text("Done")], name="planner")
    agent = make_agent(FakeJudge(task="planning"), planner=planner, plan_max_question_rounds=1)

    async def clarifier(req):
        return [ClarifyAnswer(option=0)]
    agent.bus.clarifier = clarifier
    await agent.run("Build a news app")
    tools_on_second_call = {t.name for t in planner.infos[1].function_tools}
    assert "ask_user" not in tools_on_second_call


async def test_headless_has_no_ask_user(make_agent):
    planner = Script([text("Done")], name="planner")
    agent = make_agent(FakeJudge(task="planning"), planner=planner)
    await agent.run("Build a news app")
    assert "ask_user" not in {t.name for t in planner.infos[0].function_tools}


async def test_delegation_budget_scales_with_plan_and_cap(make_agent):
    steps = [{"title": f"S{i}", "details": "d", "acceptance": ["ok"]} for i in range(1, 21)]
    planner = Script([submit(steps=steps), tool("delegate", step="S1", plan_step=1), text("Done")], name="planner")
    agent = make_agent(FakeJudge(task="planning"), planner=planner, plan_delegation_cap=30)
    await agent.run("Build a big app")
    assert "Remaining delegations: 29" in planner.sent_text()      # min(30, max(8, 40)) - 1
