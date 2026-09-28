"""End to end (fake models): routing, plan-mode data flow, outbound guard, report validation, command approval,
taint tracking, audit, rollback."""
import json
from pathlib import Path

import pytest

from hermie.agents import build_cloud_agent
from hermie.capabilities import OutboundBlockedError
from hermie.config import RunMode
from hermie.events import Approval, OutboundSent, RouteDecided, Tainted
from hermie.policy import Force
from hermie.session import TaskState

from .conftest import FakeJudge, Script, final, text, tool

PHONE, ID = "13812345678", "11010519491231002X"


async def test_repetitive_stays_local_and_writes_via_sandbox(make_agent, settings):
    ex = Script([tool("write_file", path="out/titles.md", content="# Title")],
                final=final(artifacts=[{"path": "out/titles.md", "type": "Markdown", "size_hint": "1 line"}]))
    cloud = Script(name="cloud")
    agent = make_agent(FakeJudge(task="repetitive", cx=2), executor=ex, cloud=cloud)
    r = await agent.run("Translate these 200 English titles into Chinese")
    assert r.route == "local" and r.backend == "ollama" and not cloud.seen
    assert (settings.workspace / "out/titles.md").read_text() == "# Title"
    assert "out/titles.md" in r.output and r.snapshot_id


async def test_cloud_direct_when_no_local_ops(make_agent):
    cloud = Script([text("Three-month launch plan...")], name="cloud")
    ex = Script(name="executor")
    agent = make_agent(FakeJudge(task="planning", needs_ws=False), executor=ex, cloud=cloud)
    r = await agent.run("Help me plan a three-month product launch")
    assert r.route == "cloud" and r.backend == "deepseek" and r.output.startswith("Three-month")
    assert not ex.seen and r.outbound_count == 1
    assert any(isinstance(e, OutboundSent) for e in agent.events)


async def test_plan_mode_redacts_pii_and_restores_locally(make_agent, settings):
    planner = Script([tool("delegate", step="Generate the financial plan file plan.md for the customer with <CN_MOBILE_1>"),
                      text("Done, produced plan.md")], name="planner")
    ex = Script([tool("write_file", path="plan.md", content=f"Plan for customer {PHONE}")],
                final=final(steps=["Generated 1 financial plan"],
                            artifacts=[{"path": "plan.md", "type": "Markdown", "size_hint": "1 line"}],
                            answer=f"Generated a plan for {PHONE}"))
    agent = make_agent(FakeJudge(task="planning"), executor=ex, planner=planner)
    r = await agent.run(f"Do financial planning for a customer; customer phone {PHONE}, ID card {ID}")
    assert r.route == "plan" and r.backend == "deepseek-plan+ollama"
    sent = planner.sent_text()
    assert PHONE not in sent and ID not in sent and "<CN_MOBILE_1>" in sent
    assert "Generated 1 financial plan" in sent                  # the planner got the clean report
    assert PHONE in ex.sent_text()                               # the executor got the original text and the restored step locally
    assert f"for the customer with {PHONE}" in ex.sent_text()
    assert PHONE in r.output                                     # the final result comes from the local side
    outbound = (settings.outbound_log_path).read_text()
    assert PHONE not in outbound and ID not in outbound


async def test_plan_mode_contextual_secret_uses_abstract_description(make_agent):
    planner = Script([text("Plan: ...")], name="planner")
    comp = Script([text("Abstracted task: draft a communication plan for an internal change affecting a group of employees")],
                  name="abstractor")
    agent = make_agent(FakeJudge(task="planning", secrets=("lay off",)), planner=planner, compressor=comp)
    r = await agent.run("Next quarter we need to lay off 30% of the R&D department; help me plan the communication")
    assert r.route == "plan"
    sent = planner.sent_text()
    assert "Abstracted task" in sent and "lay off" not in sent and "30%" not in sent


async def test_plan_mode_all_deidentification_fails_stays_local(make_agent):
    planner = Script(name="planner")
    comp = Script([text("It still mentions the layoff")], name="abstractor")
    agent = make_agent(FakeJudge(task="planning", secrets=("layoff",)), planner=planner, compressor=comp)
    r = await agent.run("How should we do next quarter's layoff plan")
    assert r.backend == "ollama" and not planner.seen and "de-identification failed" in "".join(r.reasons)


async def test_dirty_report_is_retried_then_stripped(make_agent, settings):
    planner = Script([tool("delegate", step="Tidy up the customer table"), text("OK")], name="planner")
    dirty = final(steps=[f"Contacted customer {PHONE}"])
    ex = Script([dirty] * (settings.report_retries + 1))
    agent = make_agent(FakeJudge(task="planning"), executor=ex, planner=planner)
    r = await agent.run("Tidy up the customer table and plan follow-up visits")
    sent = planner.sent_text()
    assert PHONE not in sent
    assert "status: done\nRemaining delegations" in sent and "steps_done" not in sent  # only status left (plus the budget line)


async def test_dirty_report_fixed_on_retry(make_agent):
    planner = Script([tool("delegate", step="Tidy up the customer table"), text("OK")], name="planner")
    ex = Script([final(steps=[f"Contacted customer {PHONE}"]), final(steps=["Contacted 1 customer"])])
    agent = make_agent(FakeJudge(task="planning"), executor=ex, planner=planner)
    await agent.run("Tidy up the customer table and plan follow-up visits")
    assert "Contacted 1 customer" in planner.sent_text() and PHONE not in planner.sent_text()


async def test_sensitive_artifact_path_is_aliased(make_agent):
    planner = Script([tool("delegate", step="Write the report"), tool("delegate", step="Check the format of file#1"),
                      text("OK")], name="planner")
    ex = Script([final(artifacts=[{"path": f"reports/{PHONE}.md", "type": "Markdown", "size_hint": "2 pages"}]),
                 final()])
    agent = make_agent(FakeJudge(task="planning"), executor=ex, planner=planner)
    await agent.run("Write a report for the customer")
    assert PHONE not in planner.sent_text() and "file#1" in planner.sent_text()
    assert f"Check the format of reports/{PHONE}.md" in ex.sent_text()   # the alias was restored locally


async def test_outbound_guard_blocks_uncertified_content(make_agent):
    agent = make_agent(FakeJudge(), cloud=Script([text("x")], name="cloud"))
    st = TaskState(agent.session, "t")
    cloud_agent = build_cloud_agent(agent.models, planning=False)
    with pytest.raises(OutboundBlockedError):
        await cloud_agent.run(f"Bypass the type check and send {PHONE} directly", deps=st)
    assert st.outbound_count == 0


async def test_cloud_failure_falls_back_local(make_agent):
    def boom(m, info):
        raise ConnectionError("network down")
    agent = make_agent(FakeJudge(task="planning", needs_ws=False), cloud=Script([boom], name="cloud"))
    r = await agent.run("Design a microservice architecture")
    assert r.backend == "ollama" and "DeepSeek unavailable" in "".join(r.reasons)


async def test_no_api_key_plan_falls_back_local(make_agent):
    agent = make_agent(FakeJudge(task="planning"), deepseek_api_key="")
    r = await agent.run("Plan and refactor this project")
    assert r.backend == "ollama" and "DEEPSEEK_API_KEY" in "".join(r.reasons)


async def test_judge_failure_routes_local(make_agent):
    cloud = Script(name="cloud")
    agent = make_agent(FakeJudge(fail_task=True), cloud=cloud)
    r = await agent.run("Design a microservice architecture")
    assert r.route == "local" and not cloud.seen


async def test_local_verify_escalates_to_cloud(make_agent):
    cloud = Script([text("CLOUD_ANSWER")], name="cloud")
    agent = make_agent(FakeJudge(task="complex", conf=0.4, cx=1, cx_conf=0.4, verify=0.1, needs_ws=False),
                       cloud=cloud)
    r = await agent.run("Analyze the time complexity of this algorithm")
    assert r.route == "local_verify" and r.backend == "deepseek" and r.output == "CLOUD_ANSWER"


async def test_local_verify_passes(make_agent):
    cloud = Script(name="cloud")
    agent = make_agent(FakeJudge(task="complex", conf=0.4, cx=1, cx_conf=0.4, verify=0.9), cloud=cloud)
    r = await agent.run("Analyze the time complexity of this algorithm")
    assert r.backend == "ollama" and not cloud.seen and "LOCAL_ANSWER" in r.output


async def test_high_risk_command_needs_approval_in_default_mode(make_agent, settings):
    (settings.workspace).mkdir(parents=True, exist_ok=True)
    (settings.workspace / "keep.txt").write_text("x")
    ex = Script([tool("run_command", command="rm keep.txt")])
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex, mode=RunMode.DEFAULT)
    asked = []

    async def deny(req):
        asked.append(req)
        return Approval.DENY
    agent.bus.approver = deny
    await agent.run("Clean up files")
    assert asked and asked[0].summary == "rm keep.txt" and asked[0].risk == "high"
    assert (settings.workspace / "keep.txt").exists()
    assert "User denied" in ex.sent_text()


async def test_auto_mode_skips_approval_but_keeps_sandbox(make_agent, settings):
    # Put the escape target in the home dir: tmp_path lives in the per-user temp dir, where toolchains may write caches
    outside = Path.home() / ".hermie_escape_test2.txt"
    outside.unlink(missing_ok=True)
    ex = Script([tool("run_command", command=f"rm -f x; echo pwned > {outside}")])
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex, mode=RunMode.AUTO)

    async def never(req):
        raise AssertionError("auto mode must not request approval")
    agent.bus.approver = never
    try:
        await agent.run("Do something")
        assert not outside.exists()  # what is skipped is the approval, not the boundary
    finally:
        outside.unlink(missing_ok=True)


async def test_taint_tracking(make_agent, settings):
    settings.workspace.mkdir(parents=True, exist_ok=True)
    (settings.workspace / "clients.csv").write_text(f"name,phone\nZhang Wei,{PHONE}\n")
    ex = Script([tool("read_file", path="clients.csv")])
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex)
    r = await agent.run("Count the rows in clients.csv")
    assert r.tainted and any(isinstance(e, Tainted) for e in agent.events)


async def test_audit_log_has_no_raw_text(make_agent, settings):
    agent = make_agent(FakeJudge(task="repetitive"))
    await agent.run(f"Phone {PHONE}, tidy up the format")
    log = settings.audit_log_path.read_text()
    assert PHONE not in log and "input_sha256" in log


async def test_force_local(make_agent):
    cloud = Script(name="cloud")
    agent = make_agent(FakeJudge(task="planning", needs_ws=False), cloud=cloud)
    r = await agent.run("Plan", force=Force.LOCAL)
    assert r.route == "local" and not cloud.seen


async def test_rollback(make_agent, settings):
    settings.workspace.mkdir(parents=True, exist_ok=True)
    (settings.workspace / "a.txt").write_text("original")
    ex = Script([tool("write_file", path="a.txt", content="broken")])
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex)
    r = await agent.run("Edit a.txt")
    assert (settings.workspace / "a.txt").read_text() == "broken"
    agent.rollback(r.snapshot_id)
    assert (settings.workspace / "a.txt").read_text() == "original"


async def test_route_event_emitted(make_agent):
    agent = make_agent(FakeJudge(task="repetitive"))
    await agent.run("Translate")
    ev = next(e for e in agent.events if isinstance(e, RouteDecided))
    assert ev.route == "local" and "privacy" in ev.signals


async def test_executor_run_timeout_yields_partial(make_agent, settings):
    ex = Script([tool("run_command", command="sleep 20")])
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex, executor_run_timeout_s=1.0, command_timeout_s=30)
    r = await agent.run("Wait a moment")
    assert r.report["status"] == "partial" and "Execution timed out" in r.report["issues"][0]


def test_trim_history_stubs_large_payloads():
    from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart

    from hermie.agents import trim_history
    big = "x" * 5000
    hist = [ModelResponse(parts=[ToolCallPart("write_file", {"path": "a.txt", "content": big})]),
            ModelRequest(parts=[ToolReturnPart("write_file", big, tool_call_id="1")])]
    out = trim_history(hist)
    assert out[0].parts[0].args["path"] == "a.txt" and "omitted" in out[0].parts[0].args["content"]
    assert len(out[1].parts[0].content) < 400 and hist[1].parts[0].content == big  # the original object is unchanged
