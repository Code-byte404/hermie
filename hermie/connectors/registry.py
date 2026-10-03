"""Which connectors exist, which are ready, and how their tools reach the executor."""
from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic_ai import RunContext, Tool
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.tools import ToolDefinition

from .asc import AscConnector
from .base import Connector, ConnectorTool, Status
from .guard import run_connector_tool

if TYPE_CHECKING:
    from ..config import Settings
    from ..session import TaskState

log = logging.getLogger(__name__)

CONNECTOR_INSTRUCTIONS = """

Business-data tools (listed below as tools) read the user's own App Store / advertising data, read-only. Calling one
keeps the whole task on this machine. Their full outputs are saved to read-only files whose paths the tool returns:
compute totals, changes and rankings from those files with python in run_command instead of estimating from the
preview."""


def build_connectors(s: "Settings") -> tuple[list[Connector], list[str]]:
    names = s.connectors
    asc_bin = s.asc_path or shutil.which("asc")
    if names is None:                       # auto: asc when installed, silently nothing otherwise
        names = ["asc"] if asc_bin else []
    out: list[Connector] = []
    notes: list[str] = []
    for n in names:
        if n == "asc":
            if not asc_bin:
                notes.append("Connector asc: the asc CLI was not found; install it or set ASC_PATH")
                continue
            out.append(AscConnector(Path(asc_bin), timeout=s.connector_timeout, preview_chars=s.connector_preview_chars))
        else:
            notes.append(f"Unknown connector {n!r} in CONNECTORS (known: asc)")
    return out, notes


def ready_connectors(conns: list[Connector]) -> tuple[list[Connector], list[str]]:
    """The connectors that can be offered. A connector whose status(), tools() or instructions() raises, or that is
    not ready, becomes a startup note; one whose tool names collide with an earlier connector's is dropped with a
    note. Never raises: a broken connector must not stop Hermie from starting."""
    ready: list[Connector] = []
    notes: list[str] = []
    taken: set[str] = set()
    for c in conns:
        name = getattr(c, "name", type(c).__name__)
        try:
            st = c.status()
            if st.state != "ready":
                notes.append(f"Connector {name} unavailable ({st.state}): {st.reason}")
                continue
            tool_names = [t.name for t in c.tools()]
            c.instructions()
        except Exception as e:
            log.exception("Connector %s failed to start", name)
            notes.append(f"Connector {name} unavailable (error): {type(e).__name__}: {e}")
            continue
        clash = sorted(set(tool_names) & taken | {n for n in tool_names if tool_names.count(n) > 1})
        if clash:
            notes.append(f"Connector {name} unavailable: tool names {clash} collide with another connector's")
            continue
        taken.update(tool_names)
        ready.append(c)
    return ready, notes


def prune_rooms(rooms_dir: Path, keep_days: int) -> None:
    """Delete data rooms older than keep_days. Fail-open: a cleanup problem is logged, never raised."""
    try:
        if not rooms_dir.is_dir():
            return
        cutoff = time.time() - keep_days * 86400
        for d in rooms_dir.iterdir():
            try:
                if d.is_dir() and d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                log.warning("Could not prune data room %s", d, exc_info=True)
    except OSError:
        log.warning("Could not prune data rooms in %s", rooms_dir, exc_info=True)


def _bind(c: Connector, t: ConnectorTool):
    async def call(ctx: RunContext["TaskState"], **args) -> str:
        return await run_connector_tool(ctx.deps, c, t, args)
    return call


def connector_tools(conns: list[Connector]) -> list[Tool]:
    return [Tool.from_schema(_bind(c, t), name=t.name, description=t.description, json_schema=t.parameters,
                             takes_ctx=True, sequential=True)
            for c in conns for t in c.tools()]


def connector_instructions(conns: list[Connector]) -> str:
    return "\n\n".join(f"## {c.title}\n{c.instructions()}" for c in conns)


@dataclass
class ConnectorScope(AbstractCapability["TaskState"]):
    """Connector tools exist only where the executor's report stays local: never inside a planner delegation."""
    names: frozenset[str]

    async def prepare_tools(self, ctx: RunContext["TaskState"], tool_defs: list[ToolDefinition]):
        if ctx.deps.report_for_cloud:
            return [t for t in tool_defs if t.name not in self.names]
        return tool_defs
