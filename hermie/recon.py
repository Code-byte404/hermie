"""Local recon before planning: directory layout, project type, toolchain, AGENT.md status.

Deterministic, no model call: runs one list and one probe command in the sandbox. The result is appended as
[Workspace overview] to the task description sent to the planner and takes the same gate path as the task text
(certify / placeholders / abstraction), so private data in file names is handled the same way.
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .sandbox import Sandbox

MAX_TOP_ENTRIES = 40

_MARKERS = [
    ("pyproject.toml", "Python project (pyproject.toml)"), ("setup.py", "Python project (setup.py)"),
    ("requirements.txt", "Python (requirements.txt)"), ("environment.yml", "conda env (environment.yml)"),
    ("package.json", "Node.js project (package.json)"), ("tsconfig.json", "TypeScript"),
    ("Cargo.toml", "Rust project (Cargo.toml)"), ("go.mod", "Go project (go.mod)"),
    ("Package.swift", "Swift Package"), ("Podfile", "CocoaPods"), ("Makefile", "has Makefile"),
    ("Dockerfile", "has Dockerfile"), ("pytest.ini", "pytest"), ("tox.ini", "tox"),
]
_PROBE = ("python3 --version 2>&1; node --version 2>&1; pandoc --version 2>&1 | head -1; "
          "swiftc --version 2>&1 | head -1; git rev-parse --is-inside-work-tree 2>&1; xcodebuild -version 2>&1 | head -1")


def summarize_files(files: list[dict], truncated: bool = False) -> list[str]:
    """Build overview lines from the list_files result: project type markers, counts by extension, top-level entries."""
    paths = [f["path"] for f in files]
    names = {Path(p).name for p in paths}
    top = sorted({p.split("/")[0] for p in paths})
    markers = [label for name, label in _MARKERS if name in names]
    if any(p.endswith(".xcodeproj/project.pbxproj") or ".xcodeproj/" in p for p in paths):
        markers.append("Xcode project")
    if any(t in ("tests", "test", "__tests__", "spec") for t in top):
        markers.append("has tests dir")
    ext = Counter((Path(p).suffix or "(no extension)") for p in paths)
    lines = []
    if markers:
        lines.append("Project type: " + ", ".join(markers))
    lines.append(f"Files: {len(paths)}{'+' if truncated else ''} files total; by type: "
                 + ", ".join(f"{k} {v}" for k, v in ext.most_common(8)))
    shown = top[:MAX_TOP_ENTRIES]
    lines.append("Top level: " + ", ".join(shown) + ("..." if len(top) > MAX_TOP_ENTRIES else ""))
    return lines


def summarize_tools(probe_output: str) -> str:
    found = []
    for line in probe_output.splitlines():
        line = line.strip()
        if not line or "not found" in line or "No such file" in line or "xcode-select" in line:
            continue
        if re.match(r"^(Python|v\d|pandoc|Apple Swift|swift|Xcode|true)", line):
            found.append("git repo" if line == "true" else line[:60])
    return "Tools: " + ("; ".join(found) if found else "no common toolchain detected")


async def workspace_recon(sandbox: "Sandbox", has_doc: bool) -> str:
    listing = await sandbox.fs("list", path=".", depth=2)
    files = listing.get("files", []) if isinstance(listing, dict) else []
    lines = summarize_files(files, bool(listing.get("truncated"))) if files else ["Files: workspace is empty"]
    try:
        r = await sandbox.run_shell(_PROBE, timeout=30)
        lines.append(summarize_tools(r.stdout))
    except Exception:
        lines.append("Tools: probe failed")
    lines.append("Project doc: " + ("has AGENT.md (see above)" if has_doc else "no AGENT.md"))
    return "[Workspace overview]\n" + "\n".join(lines)
