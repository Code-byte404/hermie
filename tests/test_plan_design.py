"""Plan mode design phase: questions, a structured plan, approval, PLAN.md, and the privacy of answers."""
import asyncio
import json

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
    await agent.run("Write a project tracker")
    sent = planner.sent_text()
    assert "merger" not in sent and "Q1: answer withheld (private)" in sent
    notices = [e.text for e in agent.events if isinstance(e, Notice)]
    assert any("Your answer to Q1 was withheld" in n for n in notices)
    record = json.loads(agent.s.trajectory_log_path.read_text().splitlines()[-1])
    assert record["sensitive"] is True


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


def revise(**over):
    args = {"goal": "A news reader", "architecture": "SwiftUI + RSS", "steps": STEPS, **over}
    return tool("revise_plan", **args)


async def test_plan_proposed_revision_numbers_count_up(make_agent):
    planner = Script([submit(), revise(goal="v2"), revise(goal="v3"), text("Done")], name="planner")
    agent = make_agent(FakeJudge(task="planning"), planner=planner)
    await agent.run("Build a news app")
    assert [e.revision for e in agent.events if isinstance(e, PlanProposed)] == [1, 2, 3]


async def test_change_request_notifies_what_was_sent(make_agent):
    planner = Script([submit(), revise(goal="v2"), revise(goal="v3"), text("Done")], name="planner")
    agent = make_agent(FakeJudge(task="planning"), planner=planner, mode=RunMode.DEFAULT)
    decisions = [PlanDecision("revise", f"Text alerts to {PHONE} too"), PlanDecision("approve")]
    revisions = []

    async def reviewer(req):
        revisions.append(req.revision)
        return decisions.pop(0)
    agent.bus.plan_reviewer = reviewer
    await agent.run("Build a news app")
    sent = planner.sent_text()
    assert PHONE not in sent and "The user asked for changes:" in sent
    notices = [e.text for e in agent.events if isinstance(e, Notice)]
    assert any(n.startswith("Your change request was sent as:") and PHONE not in n for n in notices)
    assert revisions == [2, 3]
    assert [e.revision for e in agent.events if isinstance(e, PlanProposed)] == [1, 3]


async def test_withheld_change_request_notifies(make_agent):
    planner = Script([submit(), revise(goal="v2"), text("Done")], name="planner")
    comp = Script([text("still about the merger with Acme")], name="comp")
    agent = make_agent(FakeJudge(task="planning", secrets=("merger",)), planner=planner, compressor=comp,
                       mode=RunMode.DEFAULT)

    async def reviewer(req):
        return PlanDecision("revise", "mention the secret merger with Acme")
    agent.bus.plan_reviewer = reviewer
    await agent.run("Build a news app")
    sent = planner.sent_text()
    assert "merger" not in sent and "could not be sent (private)" in sent
    notices = [e.text for e in agent.events if isinstance(e, Notice)]
    assert any("Your change request was withheld" in n for n in notices)


def _designed(make_agent, planner, mode=RunMode.DEFAULT, **kw):
    return make_agent(FakeJudge(task="planning"), planner=planner, plan_design=True, mode=mode, **kw)


async def test_design_asks_then_plan_is_approved_then_executed(make_agent):
    planner = Script([ask("Platform?"), submit(decisions=["Platform: iOS"]),
                      tool("delegate", step="Build the skeleton", plan_step=1), text("Done")], name="planner")
    agent = _designed(make_agent, planner)
    reviews = []

    async def clarifier(req):
        return [ClarifyAnswer(option=0)]

    async def reviewer(req):
        reviews.append(req)
        return PlanDecision("approve")
    agent.bus.clarifier, agent.bus.plan_reviewer = clarifier, reviewer
    r = await agent.run("Build a news app")
    assert r.route == "plan" and len(reviews) == 1 and reviews[0].revision == 1
    design_tools = {t.name for t in planner.infos[0].function_tools}
    assert "delegate" not in design_tools and "ask_user" in design_tools
    exec_tools = {t.name for t in planner.infos[2].function_tools}
    assert "delegate" in exec_tools and "revise_plan" in exec_tools
    assert "Plan approved" in planner.sent_text()
    assert [e.approved_by for e in agent.events if isinstance(e, PlanProposed)] == ["user"]


async def test_change_request_then_approve(make_agent):
    planner = Script([submit(), submit(assumptions=["Dark mode"]), text("Done")], name="planner")
    agent = _designed(make_agent, planner)
    decisions = [PlanDecision("revise", "Add dark mode"), PlanDecision("approve")]

    async def reviewer(req):
        return decisions.pop(0)
    agent.bus.plan_reviewer = reviewer
    await agent.run("Build a news app")
    assert "The user asked for changes:\nAdd dark mode" in planner.sent_text()
    assert not decisions


async def test_reject_after_revision_executes_nothing(make_agent, settings):
    planner = Script([submit(), submit(), text("never")], name="planner")
    ex = Script(final=final())
    agent = _designed(make_agent, planner, executor=ex)
    decisions = [PlanDecision("revise", "Use Flutter"), PlanDecision("reject")]

    async def reviewer(req):
        return decisions.pop(0)
    agent.bus.plan_reviewer = reviewer
    r = await agent.run("Build a news app")
    assert "Plan rejected" in r.output and not ex.seen
    assert not (settings.workspace / "PLAN.md").exists()
    from hermie.events import TaskFinished
    assert [e.status for e in agent.events if isinstance(e, TaskFinished)] == ["rejected"]


async def test_auto_mode_auto_approves_and_passes_timeout(make_agent):
    planner = Script([ask("Platform?"), submit(), text("Done")], name="planner")
    agent = _designed(make_agent, planner, mode=RunMode.AUTO, plan_auto_answer_s=7)
    seen = []

    async def clarifier(req):
        seen.append(req.timeout_s)
        return [ClarifyAnswer(option=0)]

    async def reviewer(req):
        raise AssertionError("AUTO mode must not ask for approval")
    agent.bus.clarifier, agent.bus.plan_reviewer = clarifier, reviewer
    await agent.run("Build a news app")
    assert seen == [7]
    assert [e.approved_by for e in agent.events if isinstance(e, PlanProposed)] == ["auto"]


async def test_headless_design_auto_approves_without_questions(make_agent):
    planner = Script([submit(), text("Done")], name="planner")
    agent = _designed(make_agent, planner)
    r = await agent.run("Build a news app")
    assert "ask_user" not in {t.name for t in planner.infos[0].function_tools}
    assert [e.approved_by for e in agent.events if isinstance(e, PlanProposed)] == ["auto"]
    assert r.route == "plan"


async def test_design_failure_falls_back_local(make_agent):
    def boom(m, info):
        raise RuntimeError("cloud down")
    agent = _designed(make_agent, Script([boom], name="planner"))
    r = await agent.run("Build a news app")
    assert r.backend == "ollama"


async def test_escalation_designs_without_questions(make_agent):
    """local_verify whose self-check fails with a workspace escalates to recon -> design: no questions there or later."""
    planner = Script([submit(), text("Done")], name="planner")
    agent = make_agent(FakeJudge(task="complex", conf=0.4, cx=1, cx_conf=0.4, verify=0.1, needs_ws=True),
                       planner=planner, executor=Script(final=final()), plan_design=True)
    asked = []

    async def clarifier(req):
        asked.append(req)
        return [ClarifyAnswer(option=0)]
    agent.bus.clarifier = clarifier
    r = await agent.run("Compare the two designs and fix the code")
    assert r.route == "local_verify" and r.backend.endswith("-plan+ollama")
    assert all("ask_user" not in {t.name for t in info.function_tools} for info in planner.infos)
    assert not asked


async def test_answer_phone_never_reaches_trajectory(make_agent, settings):
    planner = Script([ask("Who?"), submit(), text("Done")], name="planner")
    agent = _designed(make_agent, planner)

    async def clarifier(req):
        return [ClarifyAnswer(text=f"call {PHONE}")]
    agent.bus.clarifier = clarifier
    await agent.run("Build a news app")
    traj = (settings.data_dir / "trajectories.jsonl").read_text()
    assert PHONE not in traj and '"node": "design"' in traj and PHONE not in planner.sent_text()


async def test_plan_md_is_written_and_ticked_and_executor_sees_plan(make_agent, settings):
    planner = Script([submit(), tool("delegate", step="Build the skeleton", plan_step=1), text("Done")], name="planner")
    ex = Script([tool("write_file", path="App.swift", content="// app")], final=final())
    rev = Script([review(True)], name="reviewer")
    agent = make_agent(FakeJudge(task="planning"), planner=planner, executor=ex, reviewer=rev, verify_rounds=1)
    await agent.run("Build a news app")
    md = (settings.workspace / "PLAN.md").read_text()
    assert md.startswith("<!-- hermie-plan -->") and "- [x] 1. Skeleton app" in md and "- [ ] 2. Feed list" in md
    assert "[Approved plan]" in ex.sent_text() and "-> [ ] 1. Skeleton app" in ex.sent_text()
    assert "PLAN.md" not in rev.sent_text()


async def test_plan_step_out_of_range_ticks_nothing(make_agent, settings):
    planner = Script([submit(), tool("delegate", step="x", plan_step=9), tool("delegate", step="y", plan_step=0),
                      text("Done")], name="planner")
    agent = make_agent(FakeJudge(task="planning"), planner=planner)
    await agent.run("Build a news app")
    assert "- [x]" not in (settings.workspace / "PLAN.md").read_text()


async def test_existing_plan_goes_to_planner_through_the_gate(make_agent, settings):
    from hermie import plan_doc
    from hermie.planning import Plan, PlanStep
    settings.workspace.mkdir(parents=True, exist_ok=True)
    old = Plan(goal=f"Alerts for {PHONE}", architecture="a",
               steps=[PlanStep(title="Old step", details="d", acceptance=["ok"])])
    plan_doc.write(settings.workspace / "PLAN.md", old, [True])
    planner = Script([text("Done")], name="planner")
    agent = make_agent(FakeJudge(task="planning"), planner=planner)
    await agent.run("Continue the plan")
    sent = planner.sent_text()
    assert "[Existing plan]" in sent and "[x] 1. Old step" in sent and PHONE not in sent


async def test_user_plan_md_not_overwritten_or_sent(make_agent, settings):
    settings.workspace.mkdir(parents=True, exist_ok=True)
    (settings.workspace / "PLAN.md").write_text("my private roadmap\n")
    planner = Script([submit(), text("Done")], name="planner")
    agent = make_agent(FakeJudge(task="planning"), planner=planner)
    await agent.run("Build a news app")
    assert (settings.workspace / "PLAN.md").read_text() == "my private roadmap\n"
    assert (settings.workspace / "HERMIE_PLAN.md").exists()
    assert "my private roadmap" not in planner.sent_text()


async def test_headless_prints_plan_proposed(make_agent, capsys, monkeypatch):
    from hermie import cli
    from hermie.policy import Force
    planner = Script([submit(), text("Done")], name="planner")
    agent = make_agent(FakeJudge(task="planning"), planner=planner, plan_design=True)
    monkeypatch.setattr("hermie.core.Hermie", lambda s: agent)   # _headless imports Hermie from .core at call time
    await cli._headless(agent.s, "Build a news app", "", Force.NONE)
    events = [json.loads(l)["event"] for l in capsys.readouterr().out.splitlines() if l.startswith("{")]
    assert "PlanProposed" in events and events[-1] == "Result"
