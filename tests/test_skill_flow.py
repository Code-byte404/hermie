"""Skills in the task flow: reviewed multi-step successes become candidates, a second success activates them, active
skills reach the executor (never the planner), unhelpful ones retire."""
import json

from hermie.core import split_playbook
from hermie.events import Notice

from .conftest import FakeJudge, Script, final, review, text, tool

PLAYBOOK = """# Convert a spreadsheet to csv

## When to use
Converting an excel spreadsheet file to csv.

## Steps
1. List the files, read the spreadsheet, write the csv file.

## Verify
- The csv file exists and has a header row."""

PHONE = "13812345678"


def busy_executor(n=4):
    return Script([tool("list_files") for _ in range(n)], final=final(steps=["converted"], verification=["listed"]))


def agent_for(make_agent, compressor_text=PLAYBOOK, judge=None, **kw):
    return make_agent(judge or FakeJudge(task="repetitive"), executor=kw.pop("executor", busy_executor()),
                      reviewer=kw.pop("reviewer", Script([review(True)])),
                      compressor=Script([text(compressor_text)]), verify_rounds=kw.pop("verify_rounds", 1),
                      skills_enabled=kw.pop("skills_enabled", True), **kw)


def skills_of(agent):
    return agent.session.skills.all() if agent.session.skills else []


async def test_reviewed_multistep_success_becomes_a_candidate(make_agent, settings):
    agent = agent_for(make_agent)
    await agent.run("Convert data.xlsx to csv")
    [sk] = skills_of(agent)
    assert sk.status == "candidate" and sk.title == "Convert a spreadsheet to csv"
    assert (settings.skills_dir / f"{sk.slug}.md").exists()
    assert "Convert data.xlsx" not in (settings.skills_dir / "index.jsonl").read_text()


async def test_no_candidate_below_threshold_or_without_review(make_agent, settings):
    agent = agent_for(make_agent, executor=busy_executor(2))
    await agent.run("Convert data.xlsx to csv")
    assert skills_of(agent) == []
    agent2 = agent_for(make_agent, verify_rounds=0)
    await agent2.run("Convert data.xlsx to csv")
    assert skills_of(agent2) == []


async def test_sensitive_task_never_distills(make_agent, settings):
    agent = agent_for(make_agent)
    await agent.run(f"Convert the file for customer {PHONE} to csv")
    assert skills_of(agent) == []


async def test_playbook_failing_privacy_check_is_dropped(make_agent, settings):
    agent = agent_for(make_agent, compressor_text=PLAYBOOK.replace("header row", f"header row, call {PHONE}"))
    await agent.run("Convert data.xlsx to csv")
    assert skills_of(agent) == [] and not list(settings.skills_dir.glob("*.md"))


def test_non_playbook_output_is_dropped_or_unwrapped():
    assert split_playbook("```markdown\n" + PLAYBOOK + "\n```")[0] == "Convert a spreadsheet to csv"
    assert split_playbook("Sorry, I cannot help.") is None
    assert split_playbook("") is None
    assert split_playbook("# Title only\n\n## Steps\n1. x") is None


async def test_non_playbook_output_creates_no_candidate(make_agent, settings):
    agent = agent_for(make_agent, compressor_text="Here is a summary of what happened.")
    await agent.run("Convert data.xlsx to csv")
    assert skills_of(agent) == []


async def test_second_success_activates_and_third_task_gets_it(make_agent, settings):
    await agent_for(make_agent).run("Convert data.xlsx to csv")
    a2 = agent_for(make_agent)
    await a2.run("Convert report.xlsx to csv")
    [sk] = skills_of(a2)
    assert sk.status == "active" and sk.confirmations == 1
    ex = busy_executor()
    a3 = agent_for(make_agent, executor=ex)
    await a3.run("Convert the excel spreadsheet to csv")
    sent = ex.sent_text()
    assert "[Skills: procedures that worked before" in sent and "Convert a spreadsheet to csv" in sent
    rec = json.loads(settings.trajectory_log_path.read_text().splitlines()[-1])
    assert [n for n in rec["nodes"] if n["node"] == "recall_skills"][0]["skills"] == 1
    assert "Convert a spreadsheet" not in settings.trajectory_log_path.read_text()


async def test_planner_never_sees_skills(make_agent, settings):
    a = agent_for(make_agent)
    await a.run("Convert data.xlsx to csv")
    a.session.skills.set_status(skills_of(a)[0].id, "active")
    planner = Script([tool("delegate", step="Convert the excel spreadsheet to csv"), text("Done")], name="planner")
    ex = busy_executor()
    p = agent_for(make_agent, judge=FakeJudge(task="planning"), executor=ex)
    p.models._planner = planner.model
    await p.run("Convert the excel spreadsheet to csv")
    assert "Convert a spreadsheet to csv" in ex.sent_text()
    assert "Convert a spreadsheet to csv" not in planner.sent_text()
    assert "Convert a spreadsheet to csv" not in settings.outbound_log_path.read_text()


async def test_at_most_two_candidates_per_task(make_agent, settings):
    bodies = [PLAYBOOK.replace("Convert a spreadsheet to csv", f"Procedure {i}").replace("csv", w)
              for i, w in enumerate(["swift test", "docker network", "pytest sandbox"])]
    planner = Script([tool("delegate", step="Step one"), tool("delegate", step="Step two"),
                      tool("delegate", step="Step three"), text("Done")], name="planner")
    ex = Script([tool("list_files") for _ in range(12)], final=final())
    agent = make_agent(FakeJudge(task="planning"), planner=planner, executor=ex,
                       reviewer=Script([review(True), review(True), review(True)]),
                       compressor=Script([text(b) for b in bodies]), verify_rounds=1, skills_enabled=True)
    await agent.run("Do three things")
    assert len(skills_of(agent)) <= 2


async def test_unhelpful_skill_retires_with_notice(make_agent, settings):
    a = agent_for(make_agent)
    await a.run("Convert data.xlsx to csv")
    sk = skills_of(a)[0]
    a.session.skills.set_status(sk.id, "active")
    for _ in range(4):
        a.session.skills.feedback([sk.id], helped=False)
    b = agent_for(make_agent, reviewer=Script([review(False, ["wrong"]), review(True)]), verify_rounds=2,
                  executor=Script([tool("list_files")], final=final()))
    await b.run("Convert the excel spreadsheet to csv")
    assert b.session.skills.all()[0].status == "retired"
    assert any(isinstance(e, Notice) and "retired" in e.text and sk.title in e.text for e in b.events)


async def test_skills_disabled(make_agent, settings):
    ex = busy_executor()
    agent = agent_for(make_agent, executor=ex, skills_enabled=False)
    await agent.run("Convert data.xlsx to csv")
    assert agent.session.skills is None and not settings.skills_dir.exists()
    assert "[Skills:" not in ex.sent_text()
