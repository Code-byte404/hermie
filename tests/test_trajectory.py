"""Trajectories: one data-free line per task in data_dir/trajectories.jsonl (node timings, statuses, structured
signals; never task text, tool output, diffs or answers)."""
import json

from hermie.session import TaskState
from hermie.trajectory import task_record

from .conftest import FakeJudge, Script, final, review, tool

PHONE = "13812345678"


def _lines(settings):
    p = settings.trajectory_log_path
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def test_trace_note_merges_into_next_trace_add(make_agent):
    agent = make_agent(FakeJudge())
    st = TaskState(agent.session, "x")
    st.trace_note(passed=True, problems=2)
    st.trace_add("review", duration_s=0.5, status="done")
    st.trace_add("finish", duration_s=0.1, status="ok")
    assert st.trace == [{"node": "review", "passed": True, "problems": 2, "duration_s": 0.5, "status": "done"},
                        {"node": "finish", "duration_s": 0.1, "status": "ok"}]


async def test_task_writes_one_trajectory_line(make_agent, settings):
    agent = make_agent(FakeJudge(task="repetitive"))
    r = await agent.run("Rename the files")
    lines = _lines(settings)
    assert len(lines) == 1
    rec = lines[0]
    assert rec["route"] == "local" and rec["backend"] == "ollama" and rec["interrupted"] is False
    assert rec["input_sha256"] and rec["workspace_sha256"] and rec["latency_s"] >= 0
    assert rec["sensitive"] is False and rec["tainted"] is False and rec["force"] == "none"
    assert rec["signals"]["task_type"]["choice"] == "repetitive"
    assert isinstance(rec["nodes"], list)
    assert "Rename the files" not in json.dumps(rec)


async def test_trajectory_never_contains_task_or_review_text(make_agent, settings):
    ex = Script([tool("write_file", path="a.txt", content=f"call {PHONE}")], final=final(answer=f"Called {PHONE}"))
    rv = Script([review(False, problems=[f"the number {PHONE} is wrong"]), review(True)])
    agent = make_agent(FakeJudge(task="repetitive"), executor=ex, reviewer=rv, verify_rounds=2)
    await agent.run(f"Call the customer at {PHONE}")
    text = settings.trajectory_log_path.read_text()
    assert PHONE not in text and "Called" not in text


def test_task_record_reports_interruption(make_agent):
    from hermie.core import TaskResult
    agent = make_agent(FakeJudge())
    st = TaskState(agent.session, "x")
    st.trace_add("execute", duration_s=1.0, status="error", error="RuntimeError")
    rec = task_record(st, TaskResult("", "cancelled", "none", ["Task was interrupted or failed"]), 1.5, interrupted=True)
    assert rec["interrupted"] is True and rec["nodes"][0]["status"] == "error" and rec["reasons"] == ["Task was interrupted or failed"]
