"""Grading one answer: script checks against the task's canary ids, and a blind judge prompt for the rest."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from evals.e2e.tasks import Task
from evals.e2e.workspace import Canary, normalize

PLACEHOLDER = re.compile(r"<[A-Z_]+_\d+>")
_REF = re.compile(r"\{([a-z0-9_]+(?:\.[a-z0-9_]+)*)\}")


def rubric_ids(rubric: str) -> list[str]:
    return _REF.findall(rubric)


def render(rubric: str, canaries: dict[str, Canary]) -> str:
    return _REF.sub(lambda m: canaries[m.group(1)].value, rubric)


_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _number(s: str) -> str:
    n = s.replace(",", "")
    return n[:-3] if n.endswith(".00") else n


def _mentions(answer: str, value: str, kind: str = "") -> bool:
    """Exact or normalized containment; an amount matches by its number (`7,600`, `7600 yuan`, `$6,900`)."""
    if value in answer or normalize(value) in normalize(answer):
        return True
    if kind == "amount" and (m := _NUMBER.search(value)):
        return _number(m.group()) in {_number(x) for x in _NUMBER.findall(answer)}
    return False


def _app_tests(workdir: Path) -> bool:
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=workdir / "app",
                       capture_output=True, text=True, timeout=300)
    return r.returncode == 0


def check(task: Task, answer: str, canaries: dict[str, Canary], workdir: Path) -> dict:
    missing = [cid for cid in task.must_mention if not _mentions(answer, canaries[cid].value, canaries[cid].kind)]
    wrong = [cid for cid in task.must_not_mention if _mentions(answer, canaries[cid].value, canaries[cid].kind)]
    left = PLACEHOLDER.findall(answer)
    tests = _app_tests(Path(workdir)) if task.run_tests else None
    return {"passed": not missing and not wrong and not left and tests is not False, "missing": missing,
            "wrong": wrong, "placeholders_left": left, "tests_passed": tests}


def judge_prompt(task: Task, key: str, answer: str) -> str:
    return f"""You are grading an assistant's answer to a task about files in a work folder.

Task given to the assistant:
{task.prompt}
{"Follow-up questions: " + " / ".join(task.followups) if task.followups else ""}

What a correct answer contains (the grading key):
{key}

The assistant's final answer:
<<<
{answer}
>>>

Judge only correctness against the key: right people, right roles, right numbers, nothing swapped or invented.
Ignore style and language. Reply with PASS or FAIL on the first line and a one-line reason on the second."""


def parse_verdict(text: str) -> tuple[bool, str]:
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    if not lines or lines[0].strip("*").upper() not in ("PASS", "FAIL"):
        return False, f"unparsable: {text.strip()[:80]}"
    return lines[0].strip("*").upper() == "PASS", " ".join(lines[1:])


def passed(checks: dict, verdict: tuple[bool, str] | None) -> bool:
    return bool(checks["passed"]) and (verdict is None or verdict[0])
