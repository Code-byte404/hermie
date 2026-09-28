"""The project's AI working document AGENT.md (like Claude Code's CLAUDE.md, plus an automatic progress log).

- At the start of every task the AGENT.md in the workspace is read as the executor's project context;
  before it reaches the planner it goes through the privacy gate as usual.
- After a task the framework appends one deterministic entry (status, what was done, output, issues)
  under "Progress log", without calling a model.
- "About", "Current status" and "Next steps" are maintained jointly by the user and the executor
  (the executor can update them with edit_file).
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Optional

DOC_NAMES = ("AGENT.md", "agent.md", "AGENTS.md", "Agent.md")
MAX_DOC_CHARS = 12000
MAX_ENTRIES = 30
MAX_LESSONS = 20
PROGRESS_HEADER = "## Progress log"
LESSONS_HEADER = "## Lessons"

TEMPLATE = """# AGENT.md

This is the project's AI working document. Hermie reads it before every task and appends an entry
under "Progress log" when the task ends. "About", "Current status" and "Next steps" can be edited by hand
at any time; the executor also updates them when a task changes the project's state.

## About

(What this project does, tech stack, directory conventions, things to watch out for)

## Current status

(Where things stand right now)

## Next steps

(What to do next)

""" + LESSONS_HEADER + """

(When a fix succeeds after a failed review, the system records one line on "what works in this project";
you can also write entries by hand)

""" + PROGRESS_HEADER + "\n"


def find_doc(workspace: Path) -> Optional[Path]:
    for name in DOC_NAMES:
        p = workspace / name
        if p.is_file():
            return p
    return None


def _strip_lessons(text: str) -> str:
    """Drop the "Lessons" section (up to the next "## " heading): lessons reach the executor through recall, and
    the planner never gets them."""
    if LESSONS_HEADER not in text:
        return text
    before, _, rest = text.partition(LESSONS_HEADER)
    nxt = rest.find("\n## ")
    return before.rstrip() + ("\n\n" + rest[nxt + 1:] if nxt != -1 else "\n")


def load(workspace: Path, include_lessons: bool = True) -> str:
    """Read the document; when too long keep the head (about/status/next steps) and the tail (recent progress)."""
    p = find_doc(workspace)
    if p is None:
        return ""
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if not include_lessons:
        text = _strip_lessons(text)
    if len(text) > MAX_DOC_CHARS:
        half = MAX_DOC_CHARS // 2
        text = text[:half] + "\n\n...(middle omitted)...\n\n" + text[-half:]
    return text.strip()


def lessons(workspace: Path) -> list[str]:
    """The bullet lines of the "Lessons" section, without the "- "."""
    p = find_doc(workspace)
    if p is None:
        return []
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if LESSONS_HEADER not in text:
        return []
    section = text.partition(LESSONS_HEADER)[2]
    nxt = section.find("\n## ")
    section = section[:nxt] if nxt != -1 else section
    return [l.strip()[2:].strip() for l in section.splitlines() if l.strip().startswith("- ")]


def _one_line(s: str, limit: int) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= limit else s[:limit] + "..."


def format_entry(task: str, route_label: str, status_label: str, steps: list[str], artifacts: list[str],
                 issues: list[str], summary: str = "") -> str:
    lines = [f"### {time.strftime('%Y-%m-%d %H:%M')} · {status_label} · {route_label}",
             f"- Task: {_one_line(task, 200)}"]
    if steps:
        lines.append("- Did: " + "; ".join(_one_line(s, 80) for s in steps[:8]))
    if artifacts:
        lines.append("- Output: " + ", ".join(f"`{a}`" for a in dict.fromkeys(artifacts))[:400])
    if issues:
        lines.append("- Issues: " + "; ".join(_one_line(i, 80) for i in issues[:5]))
    if summary and not steps:
        lines.append("- Result: " + _one_line(summary, 200))
    return "\n".join(lines)


def record_progress(workspace: Path, entry: str) -> Path:
    """Append one progress entry; create the template first if the document does not exist.
    Keeps only the last MAX_ENTRIES entries."""
    p = find_doc(workspace) or workspace / DOC_NAMES[0]
    text = p.read_text(encoding="utf-8", errors="replace") if p.exists() else TEMPLATE
    if PROGRESS_HEADER not in text:
        text = text.rstrip() + "\n\n" + PROGRESS_HEADER + "\n"
    head, _, log = text.partition(PROGRESS_HEADER)
    entries = [e.strip() for e in re.split(r"\n(?=### )", log.strip()) if e.strip()]
    entries.append(entry.strip())
    entries = entries[-MAX_ENTRIES:]
    p.write_text(head + PROGRESS_HEADER + "\n\n" + "\n\n".join(entries) + "\n", encoding="utf-8")
    return p


def record_lesson(workspace: Path, lesson: str) -> Path:
    """Append one line under "Lessons" (placed before "Progress log"); insert the section if missing.
    Keeps only the last MAX_LESSONS entries."""
    p = find_doc(workspace) or workspace / DOC_NAMES[0]
    text = p.read_text(encoding="utf-8", errors="replace") if p.exists() else TEMPLATE
    if PROGRESS_HEADER not in text:
        text = text.rstrip() + "\n\n" + PROGRESS_HEADER + "\n"
    head, _, log = text.partition(PROGRESS_HEADER)
    if LESSONS_HEADER in head:
        before, _, lessons = head.partition(LESSONS_HEADER)
    else:
        before, lessons = head, ""
    items = [l.strip()[2:].strip() for l in lessons.splitlines() if l.strip().startswith("- ")]
    items = [x for x in items if x != lesson.strip()] + [lesson.strip()]
    items = items[-MAX_LESSONS:]
    block = LESSONS_HEADER + "\n\n" + "\n".join(f"- {x}" for x in items) + "\n\n"
    p.write_text(before.rstrip() + "\n\n" + block + PROGRESS_HEADER + log, encoding="utf-8")
    return p
