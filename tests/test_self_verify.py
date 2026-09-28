"""Self-verification loop: verify before reporting (deterministic rule), the local reviewer looks at the diff and makes
the executor fix things, the planner's plan / acceptance criteria / diagnosis / budget, recon, lessons."""
import json

import pytest
from pydantic_ai.messages import ModelRequest, RetryPromptPart

from hermie.agents import unverified_writes
from hermie.events import Notice, PlanUpdated, ReportArrived, ReviewArrived
from hermie.recon import summarize_files, summarize_tools

from .conftest import FakeJudge, Script, final, review, text, tool

PHONE = "13812345678"


def retry_prompts(script: Script) -> list[str]:
    """All retry prompts in the history of the last model call (each retry counted once)."""
    return [p.content for m in script.seen[-1] if isinstance(m, ModelRequest)
            for p in m.parts if isinstance(p, RetryPromptPart) and isinstance(p.content, str)]


# ---------------- A1. Verify before reporting

@pytest.mark.parametrize("seq,unverified", [
    ([], False), (["read_file"], False), (["write_file"], True), (["write_file", "run_command"], False),
    (["write_file", "read_file"], False), (["run_command", "write_file"], True),
    (["write_file", "run_command", "edit_file"], True), (["edit_file", "list_files"], True),
])
def test_unverified_writes_rule(seq, unverified):
    assert unverified_writes(seq) == unverified


async def test_done_without_verification_is_sent_back(make_agent, settings):
    ex = Script([tool("write_file", path="a.py", content="print(1)"),
                 final(artifacts=[{"path": "a.py", "type": "Python", "size_hint": "1 line"}]),   # reports done without verifying
                 tool("run_command", command="python a.py")],
                final=final(steps=["Wrote the script and ran it"],
                            artifacts=[{"path": "a.py", "type": "Python", "size_hint": "1 line"}]))
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex, verify_required=True)
    r = await agent.run("Write a script that prints 1")
    retries = retry_prompts(ex)
    assert len(retries) == 1 and "no verification was done" in retries[0]
    assert r.report["status"] == "done" and "Output not verified" not in json.dumps(r.report)
    assert "exit_code=0" in ex.sent_text()


async def test_repeated_unverified_done_is_accepted_with_issue(make_agent, settings):
    ex = Script([tool("write_file", path="a.txt", content="x")] + [final()] * (settings.report_retries + 1))
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex, verify_required=True)
    r = await agent.run("Write a file")
    assert len(retry_prompts(ex)) == settings.report_retries
    assert any("Output not verified" in x for x in r.report["issues"])


async def test_partial_report_is_not_sent_back(make_agent):
    ex = Script([tool("write_file", path="a.txt", content="x")], final=final(status="partial"))
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex, verify_required=True)
    r = await agent.run("Write a file")
    assert not retry_prompts(ex) and r.report["status"] == "partial"


# ---------------- A2/A3. Local review + fix loop

async def test_review_failure_makes_executor_fix_then_pass(make_agent, settings):
    ex = Script([tool("write_file", path="out.csv", content="a,b\n1,2\n"), tool("read_file", path="out.csv"),
                 final(steps=["Generated the table"], answer="first version"),
                 tool("edit_file", path="out.csv", old="a,b", new="a,b,c"), tool("read_file", path="out.csv"),
                 final(steps=["Added the third column"], verification=["Read the file back and checked 3 columns"],
                       answer="second version")])
    rev = Script([review(False, ["out.csv has only 2 columns; the task requires 3"], ["Add the third column c"]),
                  review(True)], name="reviewer")
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex, reviewer=rev, verify_rounds=2)
    r = await agent.run("Generate a 3-column CSV: a, b, c")
    reviews = [e for e in agent.events if isinstance(e, ReviewArrived)]
    assert [x.passed for x in reviews] == [False, True] and reviews[0].round == 1 and reviews[1].round == 2
    fix_prompt = ex.sent_text()     # the fix-round prompt reached the executor's input
    assert "Review failed (round 1)" in fix_prompt and "only 2 columns" in fix_prompt and "Add the third column" in fix_prompt
    seen_by_reviewer = rev.sent_text()
    assert "+a,b" in seen_by_reviewer and "[Workspace changes" in seen_by_reviewer     # the reviewer sees the diff
    assert "+a,b,c" in seen_by_reviewer                                                # round two sees the fixed diff
    assert (settings.workspace / "out.csv").read_text().startswith("a,b,c")
    assert "second version" in r.output and "Review failed" not in r.output
    assert agent.session.review_log and settings.review_log_path.exists()
    st_log = settings.review_log_path.read_text()
    assert "only 2 columns" in st_log


async def test_review_still_failing_after_rounds_is_reported(make_agent):
    ex = Script([final(answer="v1"), final(answer="v2")])
    rev = Script([review(False, ["missing file"]), review(False, ["still missing the file"])], name="reviewer")
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex, reviewer=rev, verify_rounds=2)
    r = await agent.run("Do something")
    reviews = [e for e in agent.events if isinstance(e, ReviewArrived)]
    assert len(reviews) == 2 and reviews[1].final and not reviews[1].passed
    assert "Local review failed" in r.output and "still missing the file" in r.output


async def test_reviewer_error_is_skipped(make_agent):
    def boom(m, info):
        raise RuntimeError("reviewer down")
    ex = Script([final(answer="ok")])
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex, reviewer=Script([boom]), verify_rounds=1)
    r = await agent.run("Do something")
    assert r.output.startswith("ok") and any("Reviewer error" in e.text for e in agent.events if isinstance(e, Notice))


async def test_review_disabled_when_rounds_zero(make_agent):
    rev = Script([review(False, ["x"])], name="reviewer")
    agent = make_agent(FakeJudge(task="repetitive"), executor=Script([final()]), reviewer=rev, verify_rounds=0)
    await agent.run("Do something")
    assert not rev.seen


async def test_review_not_run_when_executor_reports_failure(make_agent):
    rev = Script([review(False, ["x"])], name="reviewer")
    agent = make_agent(FakeJudge(task="repetitive"), executor=Script([final(status="failed")]), reviewer=rev,
                       verify_rounds=2)
    await agent.run("Do something")
    assert not rev.seen


async def test_local_verify_escalates_when_review_fails(make_agent):
    ex = Script([final(answer="v1"), final(answer="v2")])
    rev = Script([review(False, ["not good enough"]), review(False, ["still not good enough"])], name="reviewer")
    cloud = Script([text("cloud answer")], name="cloud")
    agent = make_agent(FakeJudge(task="simple", conf=0.4, cx=1, cx_conf=0.4, needs_ws=False, verify=0.9),
                       executor=ex, reviewer=rev, cloud=cloud, verify_rounds=2)
    r = await agent.run("Explain X")
    assert r.route == "local_verify" and "review failed after multiple rounds" in " ".join(r.reasons) and cloud.seen


async def test_local_verify_passes_with_review(make_agent):
    rev = Script([review(True)], name="reviewer")
    cloud = Script([text("cloud answer")], name="cloud")
    agent = make_agent(FakeJudge(task="simple", conf=0.4, cx=1, cx_conf=0.4, needs_ws=False, verify=0.9),
                       executor=Script([final(answer="local answer")]), reviewer=rev, cloud=cloud, verify_rounds=1)
    r = await agent.run("Explain X")
    assert r.route == "local_verify" and not cloud.seen and "local answer" in r.output


# ---------------- B. Planner: plan, acceptance criteria, review verdict passed back, diagnosis, budget

async def test_planner_plan_acceptance_review_and_budget(make_agent, settings):
    planner = Script([tool("set_plan", steps=["Generate the table", "Acceptance check"]),
                      tool("delegate", step="Generate out.csv with 3 columns", acceptance=["out.csv exists", "exactly 3 columns"]),
                      text("Done")], name="planner")
    ex = Script([tool("write_file", path="out.csv", content="a,b,c\n"), tool("read_file", path="out.csv"),
                 final(steps=["Generated the table"], verification=["Read back and checked 3 columns"],
                       artifacts=[{"path": "out.csv", "type": "CSV", "size_hint": "1 line"}], answer="done")])
    rev = Script([review(True)], name="reviewer")
    agent = make_agent(FakeJudge(task="planning"), executor=ex, planner=planner, reviewer=rev, verify_rounds=1,
                       max_delegations=5)
    r = await agent.run("Make a 3-column table")
    assert r.route == "plan"
    plans = [e for e in agent.events if isinstance(e, PlanUpdated)]
    assert plans[0].outline == ["Generate the table", "Acceptance check"] and plans[-1].done == [True]
    sent = planner.sent_text()
    assert "Plan recorded, 2 steps" in sent and "Remaining delegations: 4" in sent
    assert "local_review: passed" in sent and "verification:" in sent and "Read back and checked 3 columns" in sent
    assert "[Acceptance criteria]" in ex.sent_text() and "exactly 3 columns" in ex.sent_text()
    assert "[Acceptance criteria]" in rev.sent_text() and "out.csv exists" in rev.sent_text()
    reports = [e for e in agent.events if isinstance(e, ReportArrived)]
    assert reports[-1].report["local_review"]["passed"] is True


async def test_planner_gets_diagnosis_without_pii(make_agent):
    planner = Script([tool("delegate", step="Process the customer table", acceptance=["Generate out.csv"]), text("Done")],
                     name="planner")
    ex = Script([final(status="failed", issues=["runtime error"],
                       answer=f"openpyxl is not installed when reading customer{PHONE}.xlsx")])
    comp = Script([text("Missing dependency: the Python library needed to process spreadsheets is unavailable in the "
                        "sandbox; suggest using the standard library csv or converting to CSV first")],
                  name="diagnoser")
    agent = make_agent(FakeJudge(task="planning"), executor=ex, planner=planner, compressor=comp, verify_rounds=1)
    await agent.run("Tidy up the customer table")
    sent = planner.sent_text()
    assert "diagnosis: Missing dependency" in sent and PHONE not in sent and "local_review" not in sent   # no review on failure
    assert PHONE in comp.sent_text()                                                                     # the diagnosis is rewritten by the local model


async def test_dirty_diagnosis_is_dropped_not_sent(make_agent):
    planner = Script([tool("delegate", step="Process the customer table"), text("Done")], name="planner")
    ex = Script([final(status="failed", issues=["runtime error"], answer="something went wrong")])
    comp = Script([text(f"Cannot read the file for customer {PHONE}")], name="diagnoser")   # the diagnosis model did not de-identify
    agent = make_agent(FakeJudge(task="planning"), executor=ex, planner=planner, compressor=comp)
    await agent.run("Tidy up the customer table")
    sent = planner.sent_text()
    assert PHONE not in sent and "diagnosis" not in sent and "status: failed" in sent


async def test_failed_review_problems_reach_planner_when_clean(make_agent):
    planner = Script([tool("delegate", step="Write the report", acceptance=["report.md has three sections"]), text("Done")],
                     name="planner")
    ex = Script([final(answer="v1"), final(answer="v2")])
    rev = Script([review(False, ["report.md has only two sections"]), review(False, ["report.md still has only two sections"])],
                 name="reviewer")
    comp = Script([text("Incomplete content: the output is missing one section; suggest filling in each section per the "
                        "acceptance criteria")], name="diagnoser")
    agent = make_agent(FakeJudge(task="planning"), executor=ex, planner=planner, reviewer=rev, compressor=comp,
                       verify_rounds=2)
    await agent.run("Write the report")
    sent = planner.sent_text()
    assert "local_review: failed" in sent and "still has only two sections" in sent and "diagnosis: Incomplete content" in sent
    plans = [e for e in agent.events if isinstance(e, PlanUpdated)]
    assert plans[-1].done == [False]


# ---------------- B1. Recon

def test_recon_summaries():
    files = [{"path": "pyproject.toml", "size": 1}, {"path": "src/app.py", "size": 1}, {"path": "src/util.py", "size": 1},
             {"path": "tests/test_app.py", "size": 1}, {"path": "README.md", "size": 1}]
    lines = summarize_files(files)
    assert lines[0].startswith("Project type: ") and "Python project" in lines[0] and "has tests dir" in lines[0]
    assert "5 files total" in lines[1] and ".py 3" in lines[1]
    assert lines[2] == "Top level: README.md, pyproject.toml, src, tests"
    tools = summarize_tools("Python 3.12.3\nzsh: command not found: node\npandoc 3.1.11\ntrue\n")
    assert tools == "Tools: Python 3.12.3; pandoc 3.1.11; git repo"


async def test_recon_reaches_planner_through_gate(make_agent, settings):
    ws = settings.workspace
    (ws / "src").mkdir(parents=True)
    (ws / "pyproject.toml").write_text("[project]\nname='x'\n")
    (ws / "src" / "app.py").write_text("print(1)\n")
    (ws / f"customer{PHONE}.xlsx").write_bytes(b"")
    planner = Script([text("Plan: ...")], name="planner")
    agent = make_agent(FakeJudge(task="planning"), planner=planner, recon_enabled=True)
    r = await agent.run("Add tests to this project")
    assert r.route == "plan"
    sent = planner.sent_text()
    assert "[Workspace overview]" in sent and "Python project" in sent and "src" in sent and "Tools: Python" in sent
    assert PHONE not in sent and "<CN_MOBILE_1>" in sent     # the phone number in the file name went through a placeholder


# ---------------- C. Lessons

async def test_lesson_recorded_after_fix(make_agent, settings):
    ex = Script([final(answer="v1"), final(steps=["Rewrote it using the csv standard library"], answer="v2")])
    rev = Script([review(False, ["openpyxl unavailable caused the script to fail"]), review(True)], name="reviewer")
    comp = Script([text("The sandbox has no openpyxl; prefer the standard library csv for spreadsheets.")], name="lesson")
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex, reviewer=rev, compressor=comp, verify_rounds=2,
                       lessons_enabled=True)
    await agent.run("Process the spreadsheet")
    doc = (settings.workspace / "AGENT.md").read_text()
    assert "## Lessons" in doc and "- The sandbox has no openpyxl" in doc and doc.index("## Lessons") < doc.index("## Progress log")
    assert "openpyxl unavailable" in comp.sent_text() and "Rewrote it using the csv standard library" in comp.sent_text()
    # The same lesson is not recorded twice; the executor sees it on the next task
    ex2 = Script([final(answer="v1"), final(answer="v2")])
    agent2 = make_agent(FakeJudge(task="repetitive"), executor=ex2, reviewer=Script([review(False, ["x"]), review(True)]),
                        compressor=Script([text("The sandbox has no openpyxl; prefer the standard library csv for spreadsheets.")]),
                        verify_rounds=2)
    await agent2.run("Process it again")
    assert (settings.workspace / "AGENT.md").read_text().count("- The sandbox has no openpyxl") == 1
    assert "The sandbox has no openpyxl" in ex2.sent_text()


async def test_no_lesson_without_fix(make_agent, settings):
    comp = Script([text("should not be called")], name="lesson")
    agent = make_agent(FakeJudge(task="repetitive"), executor=Script([final()]), reviewer=Script([review(True)]),
                       compressor=comp, verify_rounds=1, lessons_enabled=True)
    await agent.run("Do something")
    lessons = (settings.workspace / "AGENT.md").read_text().split("## Lessons")[1].split("## Progress log")[0]
    assert not comp.seen and not [l for l in lessons.splitlines() if l.startswith("- ")]


# ---------------- Plan mode snapshots before each delegation: the review sees only this step's changes; rollback defaults to pre-task

async def test_per_step_snapshot_limits_review_diff_and_rollback_targets_task(make_agent, settings):
    planner = Script([tool("delegate", step="Write a.txt"), tool("delegate", step="Write b.txt"), text("Done")], name="planner")
    ex = Script([tool("write_file", path="a.txt", content="AAA"), tool("read_file", path="a.txt"),
                 final(artifacts=[{"path": "a.txt", "type": "txt", "size_hint": "1 line"}]),
                 tool("write_file", path="b.txt", content="BBB"), tool("read_file", path="b.txt"),
                 final(artifacts=[{"path": "b.txt", "type": "txt", "size_hint": "1 line"}])])
    rev = Script([review(True), review(True)], name="reviewer")
    agent = make_agent(FakeJudge(task="planning"), executor=ex, planner=planner, reviewer=rev, verify_rounds=1)
    r = await agent.run("Write two files")
    second_review = rev.seen[1][-1].parts[0].content
    assert "BBB" in second_review and "AAA" not in second_review          # the second step's review sees only the second step's diff
    snaps = agent.session.snapshots.list()
    assert [x.label for x in snaps] == ["task", "step", "step"] and r.snapshot_id == snaps[0].id
    from hermie.events import SnapshotTaken
    assert [e.label for e in agent.events if isinstance(e, SnapshotTaken)] == ["task", "step", "step"]
    agent.rollback()                                                       # no argument: back to pre-task, not to before the last step
    assert not (settings.workspace / "a.txt").exists() and not (settings.workspace / "b.txt").exists()


async def test_review_log_has_task_and_route(make_agent, settings):
    agent = make_agent(FakeJudge(task="repetitive"), executor=Script([final()]), reviewer=Script([review(True)]),
                       verify_rounds=1)
    await agent.run("Do something")
    rec = json.loads(settings.review_log_path.read_text().splitlines()[-1])
    assert rec["route"] == "local" and len(rec["task"]) == 12 and rec["passed"] is True
