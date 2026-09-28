"""Lessons in the task flow: recorded after review-then-fix and after unresolved repeated problems, recalled into
the executor prompt (never the planner's), fed back, and reconciled with AGENT.md."""
from hermie import project_doc
from hermie.graph import problem_key

from .conftest import FakeJudge, Script, final, review, text, tool

LESSON = "Use csv instead of openpyxl for spreadsheet files."


async def _learn(make_agent, **kw):
    ex = Script([final(answer="v1"), final(steps=["Rewrote it with csv"], answer="v2")])
    rev = Script([review(False, ["openpyxl missing"]), review(True)])
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex, reviewer=rev, compressor=Script([text(LESSON)]),
                       verify_rounds=2, lessons_enabled=True, **kw)
    await agent.run("Process the spreadsheet")
    return agent


async def test_lesson_goes_to_store_with_tags(make_agent, settings):
    agent = await _learn(make_agent)
    [l] = agent.session.lessons.all()
    assert l.text == LESSON and l.source == "review_fixed" and l.task_type == "repetitive" and l.embedding
    assert "Process the spreadsheet" not in settings.lessons_path.read_text()


async def test_recalled_lessons_reach_the_executor_and_trace(make_agent, settings):
    await _learn(make_agent)
    ex = Script([final()])
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex, verify_rounds=0, lessons_enabled=True)
    await agent.run("Convert the spreadsheet to csv")
    sent = ex.sent_text()
    assert "[Lessons from earlier tasks]" in sent and LESSON in sent
    assert "## Lessons" not in sent  # the AGENT.md section itself is no longer pasted in
    import json
    rec = json.loads(settings.trajectory_log_path.read_text().splitlines()[-1])
    assert [n for n in rec["nodes"] if n["node"] == "recall_lessons"][0]["lessons"] == 1


async def test_feedback_counts_use_and_help(make_agent, settings):
    agent = await _learn(make_agent)
    agent2 = make_agent(FakeJudge(task="repetitive"), executor=Script([final()]), reviewer=Script([review(True)]),
                        verify_rounds=1, lessons_enabled=True)
    agent2.session.lessons = agent.session.lessons
    await agent2.run("Convert the spreadsheet to csv")
    [l] = agent.session.lessons.all()
    assert l.uses == 1 and l.helped == 1


async def test_planner_never_sees_lessons(make_agent, settings):
    await _learn(make_agent)
    planner = Script([tool("delegate", step="Convert the spreadsheet"), text("Done")], name="planner")
    ex = Script([final()])
    agent = make_agent(FakeJudge(task="planning"), planner=planner, executor=ex, verify_rounds=0, lessons_enabled=True)
    await agent.run("Convert the spreadsheet to csv")
    assert LESSON in ex.sent_text()
    assert LESSON not in planner.sent_text() and "## Lessons" not in planner.sent_text()
    assert LESSON not in settings.outbound_log_path.read_text()


async def test_repeated_unresolved_problem_becomes_a_lesson(make_agent, settings):
    ex = Script([final(), final(), final()])
    rev = Script([review(False, ["The build fails: missing import os. Details follow"]),
                  review(False, ["The build fails: missing import os. Other details"]),
                  review(False, ["The build fails: missing import os."])])
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex, reviewer=rev, verify_rounds=3, lessons_enabled=True)
    await agent.run("Fix the build")
    texts = [l.text for l in agent.session.lessons.all()]
    assert texts == ["Raised 3 times by the reviewer and not resolved: The build fails: missing import os."]
    assert agent.session.lessons.all()[0].source == "repeated_failure"
    assert "- Raised 3 times" in (settings.workspace / "AGENT.md").read_text()


def test_problem_key_normalizes_first_sentence():
    assert problem_key("The build fails: missing import os. Details") == problem_key("the build  fails: missing import os.")
    assert problem_key("Line 12 is wrong.") == problem_key("Line 40 is wrong. x")


async def test_deleted_doc_lesson_is_disabled(make_agent, settings):
    agent = await _learn(make_agent)
    doc = settings.workspace / "AGENT.md"
    doc.write_text(doc.read_text().replace(f"- {LESSON}\n", ""))
    ex = Script([final()])
    agent2 = make_agent(FakeJudge(task="repetitive"), executor=ex, verify_rounds=0, lessons_enabled=True)
    agent2.session.lessons = agent.session.lessons
    await agent2.run("Convert the spreadsheet to csv")
    assert LESSON not in ex.sent_text() and agent.session.lessons.all()[0].disabled


async def test_full_doc_does_not_disable(make_agent, settings):
    agent = await _learn(make_agent)
    for i in range(project_doc.MAX_LESSONS):
        project_doc.record_lesson(settings.workspace, f"filler lesson number {i}")
    assert LESSON not in project_doc.lessons(settings.workspace)  # trimmed out by the cap
    agent2 = make_agent(FakeJudge(task="repetitive"), executor=Script([final()]), verify_rounds=0, lessons_enabled=True)
    agent2.session.lessons = agent.session.lessons
    await agent2.run("Convert the spreadsheet to csv")
    assert not next(l for l in agent.session.lessons.all() if l.text == LESSON).disabled


async def test_embedder_failure_notice_once(make_agent, settings):
    from hermie.events import Notice
    from hermie.memory import LessonStore

    class Dead:
        failed = False

        def embed(self, text):
            self.failed = True
            return None
    agent = make_agent(FakeJudge(task="repetitive"), executor=Script([final(), final()]), verify_rounds=0,
                       lessons_enabled=True)
    agent.session.lessons = LessonStore(settings.lessons_path, Dead())
    agent.session.lessons.add(LESSON, workspace="elsewhere", task_type="", tools=[], source="manual")
    await agent.run("one")
    await agent.run("two")
    notices = [e for e in agent.events if isinstance(e, Notice) and "embedding" in e.text.lower()]
    assert len(notices) == 1


async def test_lessons_disabled_means_no_recall_and_no_store(make_agent, settings):
    ex = Script([final()])
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex, verify_rounds=0, lessons_enabled=False)
    await agent.run("Convert the spreadsheet")
    assert "[Lessons from earlier tasks]" not in ex.sent_text() and not settings.lessons_path.exists()


async def test_missing_agent_md_does_not_disable_lessons(make_agent, settings):
    agent = await _learn(make_agent)
    (settings.workspace / "AGENT.md").unlink()
    ex = Script([final()])
    agent2 = make_agent(FakeJudge(task="repetitive"), executor=ex, verify_rounds=0, lessons_enabled=True)
    agent2.session.lessons = agent.session.lessons
    await agent2.run("Convert the spreadsheet to csv")
    assert LESSON in ex.sent_text() and not agent.session.lessons.all()[0].disabled


async def test_lessons_disabled_executor_still_reads_doc_lessons_planner_never(make_agent, settings):
    planner = Script([tool("delegate", step="Do it"), text("Done")], name="planner")
    ex = Script([final()])
    agent = make_agent(FakeJudge(task="planning"), planner=planner, executor=ex, verify_rounds=0, lessons_enabled=False)
    project_doc.record_lesson(settings.workspace, "Hand written: run pytest -q before reporting done.")
    await agent.run("Build the thing")
    assert "Hand written: run pytest" in ex.sent_text()
    assert "Hand written: run pytest" not in planner.sent_text()


async def test_recall_failure_does_not_abort_the_task(make_agent, settings):
    agent = await _learn(make_agent)

    def boom(*a, **k):
        raise ZeroDivisionError("bad lesson file")
    agent.session.lessons.recall = boom
    ex = Script([final(answer="still done")])
    agent.executor = agent.executor  # unchanged
    agent2 = make_agent(FakeJudge(task="repetitive"), executor=ex, verify_rounds=0, lessons_enabled=True)
    agent2.session.lessons = agent.session.lessons
    r = await agent2.run("Convert the spreadsheet to csv")
    assert r.output == "still done"
