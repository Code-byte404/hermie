"""The connector protocol. A connector sees only ConnectorContext: no TaskState, bus, gate or settings."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Literal, Optional, Protocol


@dataclass(frozen=True)
class Status:
    state: Literal["ready", "missing", "not_authenticated", "error"]
    reason: str = ""          # shown in the startup notes, with the fix


@dataclass(frozen=True)
class ConnectorResult:
    preview: str                       # what the model sees (capped)
    files: tuple[Path, ...] = ()       # full outputs, saved in the task's data room
    label: str = ""                    # command path for the local log, e.g. "reviews list" (no data)
    ok: bool = True


class ConnectorContext(Protocol):
    async def choose(self, prompt: str, options: list[str]) -> Optional[str]: ...
    async def ask(self, prompt: str) -> Optional[str]: ...
    def room_path(self, name: str) -> Path: ...     # a fresh file path in this task's data room
    def state(self) -> dict: ...                     # persistent per connector; saved after the call
    def session_state(self) -> dict: ...             # this session only; cleared by /new


@dataclass(frozen=True)
class ConnectorTool:
    name: str                 # unique across connectors, e.g. "asc"
    description: str          # short; offered in every local task
    parameters: dict          # JSON schema of the arguments
    call: Callable[[dict, ConnectorContext], Awaitable[ConnectorResult]]


class Connector(Protocol):
    name: str
    title: str

    def status(self) -> Status: ...
    def instructions(self) -> str: ...
    def tools(self) -> list[ConnectorTool]: ...
