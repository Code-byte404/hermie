"""Regression tests for the skill-library review: learning never blocks or endangers the task record, the store
survives read-only files and renames, merging uses the step as well as the playbook, no-embedding mode is explicit,
episodes carry the task line not the attachments."""
import asyncio
import os
import stat

from hermie.events import Notice, TaskFinished
from hermie.memory import LessonStore
from hermie.skills import SkillStore, parse_skill

from .conftest import FakeEmbedder, FakeJudge, Script, final, review, text, tool
from .test_skill_flow import PLAYBOOK, busy_executor

BODY = "## When to use\ncsv\n\n## Steps\n1. csv\n\n## Verify\n- csv"


def make(make_agent, **kw):
    return make_agent(FakeJudge(task="repetitive"), executor=kw.pop("executor", busy_executor()),
                      reviewer=Script([review(True)]), compressor=kw.pop("compressor", Script([text(PLAYBOOK)])),
                      verify_rounds=1, skills_enabled=True, **kw)


# ---------------- Critical: post-task learning runs after the task record, in the background

async def test_learning_runs_after_the_record_and_does_not_block(make_agent, settings):
    agent = make(make_agent)
    agent.learn_in_background = True
    started = asyncio.Event()

    async def slow_learn(st):
        started.set()
        await asyncio.sleep(3600)
    agent._skills_after_task = slow_learn
    r = await asyncio.wait_for(agent.run("Convert data.xlsx to csv"), 30)
    assert r.output and settings.audit_log_path.exists() and settings.trajectory_log_path.exists()
    assert any(isinstance(e, TaskFinished) for e in agent.events)
    await asyncio.wait_for(started.wait(), 5)
    await agent.cancel_learning()
    assert not agent._learning


async def test_interrupting_learning_keeps_the_record(make_agent, settings):
    agent = make(make_agent)   # learn_in_background False in tests: learning is awaited at the very end of run()
    async def slow_learn(st):
        await asyncio.sleep(3600)
    agent._skills_after_task = slow_learn
    task = asyncio.ensure_future(agent.run("Convert data.xlsx to csv"))
    for _ in range(300):
        await asyncio.sleep(0.05)
        if any(isinstance(e, TaskFinished) for e in agent.events):
            break
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert settings.audit_log_path.exists() and settings.trajectory_log_path.exists()
    assert any(isinstance(e, TaskFinished) for e in agent.events)


# ---------------- Important 1: unreadable or read-only skill files never stop Hermie

async def test_read_only_manual_skill_is_imported_without_rewrite(make_agent, settings):
    settings.skills_dir.mkdir(parents=True, exist_ok=True)
    p = settings.skills_dir / "mine.md"
    p.write_text("---\ntitle: My csv routine\n---\n\n" + BODY + "\n")
    p.chmod(stat.S_IRUSR)
    try:
        agent = make(make_agent)
        [sk] = agent.session.skills.all()
        assert sk.title == "My csv routine" and sk.status == "active"
    finally:
        p.chmod(stat.S_IRUSR | stat.S_IWUSR)


async def test_unreadable_skills_dir_disables_skills_with_notice(make_agent, settings):
    settings.skills_dir.mkdir(parents=True, exist_ok=True)
    (settings.skills_dir / "index.jsonl").write_text("")
    settings.skills_dir.chmod(0)
    try:
        agent = make(make_agent)
        assert agent.session.skills is None
        assert agent.startup_notes and "skill" in agent.startup_notes[0].lower()
    finally:
        settings.skills_dir.chmod(stat.S_IRWXU)


# ---------------- Important 2: renaming a skill file keeps it intact

def test_renamed_file_keeps_body_and_status(tmp_path):
    from hermie.config import Settings
    s = Settings(data_dir=tmp_path / "d")
    st = SkillStore(s.skills_dir, FakeEmbedder(), s)
    sk, _ = st.add_candidate("Convert a spreadsheet to csv", BODY, workspace="w", task_type="x")
    old = st.path(sk)
    new = old.with_name("my-csv-routine.md")
    os.rename(old, new)                        # keeps mtime, like mv / Finder
    st.sync()
    assert st.body(sk) and st.path(sk) == new
    st.set_status(sk.id, "active")
    assert not old.exists() and parse_skill(new.read_text())[0]["status"] == "active"
    st.sync()
    assert st.all()[0].status == "active"


# ---------------- Important 3: the step text confirms a skill when the playbooks are worded differently

def test_similar_step_confirms_despite_different_wording(tmp_path):
    from hermie.config import Settings
    s = Settings(data_dir=tmp_path / "d")
    st = SkillStore(s.skills_dir, FakeEmbedder(), s)
    # playbooks share 3 of 4 terms (cos 0.75, below SKILL_MERGE_SIM); the steps are the same (cos 1.0)
    a_body = "## When to use\nspreadsheet excel\n\n## Steps\n1. file\n\n## Verify\n- csv"
    b_body = "## When to use\nspreadsheet python\n\n## Steps\n1. file\n\n## Verify\n- csv"
    a, _ = st.add_candidate("A", a_body, workspace="w", task_type="x", key_text="convert spreadsheet to csv")
    b, merged = st.add_candidate("B", b_body, workspace="w", task_type="x", key_text="convert spreadsheet to csv")
    assert merged and b.id == a.id and b.status == "active"
    c_body = "## When to use\ndocker network\n\n## Steps\n1. docker\n\n## Verify\n- network"
    c, merged = st.add_candidate("C", c_body, workspace="w", task_type="x", key_text="convert spreadsheet to csv")
    assert not merged and c.id != a.id   # same step wording, unrelated procedure: no merge


# ---------------- Important 4: without embeddings, skills pause and the user is told

class Dead:
    failed = False

    def embed(self, text):
        self.failed = True
        return None


async def test_no_embeddings_pauses_skills_with_notice(make_agent, settings):
    agent = make(make_agent, lessons_enabled=False)
    store = agent.session.skills
    sk, _ = store.add_candidate("Convert a spreadsheet to csv", BODY, workspace="w", task_type="x")
    store.set_status(sk.id, "active")
    store.embedder = Dead()
    assert store.recall("convert the spreadsheet to csv", k=2, min_sim=0.0) == []
    await agent.run("Convert data.xlsx to csv")
    notes = [e.text for e in agent.events if isinstance(e, Notice) and "embedding" in e.text.lower()]
    assert len(notes) == 1 and "skill" in notes[0].lower()


# ---------------- Important 5: episodes carry the task line, not the attachments

async def test_episode_uses_task_line_not_material(make_agent, settings):
    comp = Script([text(PLAYBOOK)])
    agent = make(make_agent, compressor=comp)
    await agent.run("Convert data.xlsx to csv", "[File: /tmp/notes.txt]\nMATERIAL_MARKER " + "x " * 50)
    assert "Convert data.xlsx to csv" in comp.sent_text() and "MATERIAL_MARKER" not in comp.sent_text()
