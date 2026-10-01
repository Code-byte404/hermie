"""The plan file in the workspace (PLAN.md): rendering, safe writing, and the views of it other parts need.

Written by the main process (never through the sandbox). A file without MARKER on its first line belongs to the user
and is never overwritten or read; FALLBACK_NAME is used instead."""
from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .planning import Plan

MARKER = "<!-- hermie-plan -->"
FALLBACK_NAME = "HERMIE_PLAN.md"


def _ours(path: Path) -> bool:
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            return f.readline().strip() == MARKER
    except OSError:
        return False


def target(workspace: Path, name: str) -> Path:
    """Where to write the plan: `name`, unless a file of that name exists and is the user's own."""
    path = workspace / name
    if path.exists() and not _ours(path):
        return workspace / FALLBACK_NAME
    return path


def _bullets(items: list[str]) -> list[str]:
    return [f"- {x}" for x in items] or ["- (none)"]


def render(plan: "Plan", done: list[bool]) -> str:
    lines = [MARKER, "# Plan", "", "## Goal", plan.goal, "", "## Decisions", *_bullets(plan.decisions), "",
             "## Assumptions", *_bullets(plan.assumptions), "", "## Architecture", plan.architecture, "", "## Steps"]
    for i, step in enumerate(plan.steps, 1):
        tick = "x" if i <= len(done) and done[i - 1] else " "
        lines.append(f"- [{tick}] {i}. {step.title}")
        lines.append(f"  {step.details}")
        if step.depends_on:
            lines.append("  after step " + ", ".join(str(d) for d in step.depends_on))
        if step.files:
            lines.append("  files: " + ", ".join(step.files))
        lines += [f"  - accept: {a}" for a in step.acceptance]
    lines += ["", "## Risks", *_bullets(plan.risks), "", "## Out of scope", *_bullets(plan.out_of_scope), ""]
    return "\n".join(lines)


def write(path: Path, plan: "Plan", done: list[bool]) -> None:
    path.write_text(render(plan, done), encoding="utf-8")


def existing(workspace: Path, name: str) -> str:
    """Goal and step titles with tick state of a Hermie plan file in the workspace ("" if there is none)."""
    for path in (workspace / name, workspace / FALLBACK_NAME):
        if path.is_file() and _ours(path):
            text = path.read_text(encoding="utf-8", errors="replace")
            goal = re.search(r"^## Goal\n(.+)$", text, re.M)
            steps = re.findall(r"^- \[([ x])\] (\d+\. .+)$", text, re.M)
            return "\n".join([f"Goal: {goal.group(1) if goal else ''}"] + [f"[{t}] {s}" for t, s in steps])
    return ""


def executor_block(plan: "Plan", done: list[bool], current: Optional[int]) -> str:
    lines = ["[Approved plan]"]
    for i, step in enumerate(plan.steps, 1):
        tick = "x" if i <= len(done) and done[i - 1] else " "
        lines.append(f"{'-> ' if i == current else '   '}[{tick}] {i}. {step.title}")
    if current is not None and 1 <= current <= len(plan.steps):
        step = plan.steps[current - 1]
        lines.append(f"Current step details: {step.details}")
        if step.files:
            lines.append("Files: " + ", ".join(step.files))
    return "\n".join(lines)


_DIFF_HEAD = re.compile(r"^diff (?:--git a/|-ruN \S*?/)(\S+)")


def strip_from_diff(diff: str, name: str) -> str:
    """Drop the file sections of `name` from a unified diff (git or diff -ruN form)."""
    out, skip = [], False
    for line in diff.splitlines(keepends=True):
        m = _DIFF_HEAD.match(line)
        if m:
            skip = m.group(1) == name or m.group(1).endswith("/" + name)
        if not skip:
            out.append(line)
    return "".join(out)


def diff_steps(old: Optional["Plan"], new: "Plan") -> dict:
    """What a revision changed, by step title: 1-based indexes into `new`, titles for removed steps."""
    if old is None:
        return {"added": [], "changed": [], "removed": []}
    before = {s.title: s for s in old.steps}
    added = [i for i, s in enumerate(new.steps, 1) if s.title not in before]
    changed = [i for i, s in enumerate(new.steps, 1) if s.title in before and s != before[s.title]]
    titles = {s.title for s in new.steps}
    return {"added": added, "changed": changed, "removed": [s.title for s in old.steps if s.title not in titles]}
