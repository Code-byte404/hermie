"""Skill library store: Markdown playbooks under skills_dir plus index.jsonl for status, counters and embeddings."""
import os
import time

import pytest

from hermie.config import Settings
from hermie.skills import SkillStore, parse_skill

from .conftest import FakeEmbedder

BODY_CSV = """## When to use
Converting an excel spreadsheet file to csv.

## Steps
1. Read the spreadsheet with the python csv module after exporting.
2. Write the csv file.

## Verify
- `python -c "import csv"` and the csv file exists."""

BODY_SWIFT = """## When to use
Adding a unit test target to a swift package.

## Steps
1. Add the test target in Package.swift.
2. Run swift build and swift test.

## Verify
- `swift test` passes on the simulator build."""


def store(tmp_path, **kw):
    s = Settings(data_dir=tmp_path / "d", **kw)
    return SkillStore(s.skills_dir, FakeEmbedder(), s), s


def test_candidate_written_as_markdown_with_front_matter(tmp_path):
    st, s = store(tmp_path)
    sk, merged = st.add_candidate("Convert a spreadsheet to csv", BODY_CSV, workspace="w1", task_type="repetitive",
                                  key_text="Convert data.xlsx to csv")
    assert not merged and sk.status == "candidate" and sk.source == "distilled"
    text = st.path(sk).read_text()
    front, body = parse_skill(text)
    assert front["id"] == sk.id and front["status"] == "candidate" and front["title"] == "Convert a spreadsheet to csv"
    assert "## Steps" in body and "data.xlsx" not in (s.skills_dir / "index.jsonl").read_text()


def test_similar_candidate_confirms_and_activates(tmp_path):
    st, _ = store(tmp_path)
    a, _ = st.add_candidate("Convert a spreadsheet to csv", BODY_CSV, workspace="w1", task_type="x")
    b, merged = st.add_candidate("Spreadsheet to csv conversion", BODY_CSV + "\n", workspace="w2", task_type="x")
    assert merged and b.id == a.id and b.status == "active" and b.confirmations == 1
    assert parse_skill(st.path(b).read_text())[0]["status"] == "active"
    assert len(st.all()) == 1


def test_recall_active_only_threshold_applies_to_same_project(tmp_path):
    st, _ = store(tmp_path)
    cand, _ = st.add_candidate("Swift test target", BODY_SWIFT, workspace="w1", task_type="x")
    act, _ = st.add_candidate("Convert a spreadsheet to csv", BODY_CSV, workspace="w1", task_type="x")
    st.set_status(act.id, "active")
    got = st.recall("convert the excel spreadsheet to csv", k=2, min_sim=0.6)
    assert [x.id for x in got] == [act.id]                                   # the candidate is never recalled
    assert st.recall("run the docker network build", k=2, min_sim=0.6) == []  # dissimilar, even though same project
    st.set_status(cand.id, "active")
    assert len(st.recall("swift test and csv spreadsheet", k=1, min_sim=0.1)) == 1


def test_feedback_retires_unhelpful_and_restore(tmp_path):
    st, _ = store(tmp_path, skill_retire_uses=5, skill_retire_rate=0.3)
    sk, _ = st.add_candidate("Convert a spreadsheet to csv", BODY_CSV, workspace="w1", task_type="x")
    st.set_status(sk.id, "active")
    retired = []
    for i in range(5):
        retired += st.feedback([sk.id], helped=(i == 0))
    assert [r.id for r in retired] == [sk.id]
    assert st.all()[0].status == "retired" and parse_skill(st.path(sk).read_text())[0]["status"] == "retired"
    assert st.recall("convert the spreadsheet to csv", k=2, min_sim=0.1) == []
    st.set_status(sk.id[:5], "active")
    assert st.all()[0].status == "active"


def test_set_status_by_prefix(tmp_path):
    st, _ = store(tmp_path)
    a, _ = st.add_candidate("Convert a spreadsheet to csv", BODY_CSV, workspace="w1", task_type="x")
    with pytest.raises(LookupError):
        st.set_status("zzzz", "active")
    with pytest.raises(LookupError):
        st.set_status("", "active")
    with pytest.raises(ValueError):
        st.set_status(a.id, "bogus")


def test_manual_file_is_imported_as_active(tmp_path):
    st, s = store(tmp_path)
    (s.skills_dir / "my-skill.md").write_text("---\ntitle: My csv routine\n---\n\n" + BODY_CSV + "\n")
    assert st.sync()
    [sk] = st.all()
    assert sk.source == "manual" and sk.status == "active" and sk.title == "My csv routine" and sk.embedding
    assert "id: " in (s.skills_dir / "my-skill.md").read_text()  # id written back so the file keeps its identity


def test_edited_file_is_reembedded_and_status_applies(tmp_path):
    st, s = store(tmp_path)
    sk, _ = st.add_candidate("Convert a spreadsheet to csv", BODY_CSV, workspace="w1", task_type="x")
    st.set_status(sk.id, "active")
    assert not st.sync()                              # Hermie's own write is not a user edit
    p = st.path(sk)
    time.sleep(0.01)
    p.write_text(p.read_text().replace("status: active", "status: retired"))
    os.utime(p, (time.time() + 5, time.time() + 5))
    assert st.sync() and st.all()[0].status == "retired"
    text = p.read_text().replace("status: retired", "status: active").replace("csv", "swift test")
    p.write_text(text)
    os.utime(p, (time.time() + 10, time.time() + 10))
    st.sync()
    assert st.recall("swift test", k=1, min_sim=0.3)[0].id == sk.id


def test_deleted_file_drops_the_skill(tmp_path):
    st, _ = store(tmp_path)
    sk, _ = st.add_candidate("Convert a spreadsheet to csv", BODY_CSV, workspace="w1", task_type="x")
    st.path(sk).unlink()
    st.sync()
    assert st.all() == []


def test_malformed_files_are_skipped_not_deleted(tmp_path):
    st, s = store(tmp_path)
    junk = {"README.md": "# notes\nnothing here", "half.md": "---\ntitle: x\n---\n\n## Steps\n1. only steps\n",
            "nofront.md": BODY_CSV}
    for name, text in junk.items():
        (s.skills_dir / name).write_text(text)
    st.sync()
    assert st.all() == [] and all((s.skills_dir / n).read_text() == t for n, t in junk.items())


def test_two_stores_keep_each_others_skills(tmp_path):
    a, s = store(tmp_path)
    b = SkillStore(s.skills_dir, FakeEmbedder(), s)
    a.add_candidate("Convert a spreadsheet to csv", BODY_CSV, workspace="w1", task_type="x")
    b.add_candidate("Swift test target", BODY_SWIFT, workspace="w2", task_type="x")
    titles = sorted(x.title for x in SkillStore(s.skills_dir, FakeEmbedder(), s).all())
    assert titles == ["Convert a spreadsheet to csv", "Swift test target"]
