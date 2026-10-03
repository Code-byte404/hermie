"""Hermie's side of every connector call: flag the task as business before the call, give the connector its
context (pickers, data room, state), log the call without data, and turn any failure into a message for the model.
A connector never sees TaskState; this is the only place connector calls meet Hermie's rules."""
from __future__ import annotations

import copy
import fcntl
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from ..events import ChoiceRequest, InputRequest
from .base import Connector, ConnectorResult, ConnectorTool

if TYPE_CHECKING:
    from ..session import TaskState

log = logging.getLogger(__name__)


class StateStore:
    """data_dir/connectors/<name>.json. Saves merge with the file under an flock: two Hermie instances may share it."""

    def __init__(self, directory: Path):
        self.dir = Path(directory)

    def _path(self, name: str) -> Path:
        return self.dir / f"{name}.json"

    def load(self, name: str) -> dict:
        try:
            return json.loads(self._path(name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save(self, name: str, data: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.dir / f".{name}.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            merged = {**self.load(name), **data}
            tmp = self._path(name).with_suffix(".tmp")
            tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self._path(name))


class ConnectorGuard:
    """The ConnectorContext of one connector call."""

    def __init__(self, st: "TaskState", connector_name: str):
        self.st, self.name = st, connector_name
        self._state: Optional[dict] = None
        self._loaded: dict = {}

    async def choose(self, prompt: str, options: list[str]) -> Optional[str]:
        return await self.st.bus.request_choice(ChoiceRequest(prompt, list(options)))

    async def ask(self, prompt: str) -> Optional[str]:
        return await self.st.bus.request_input(InputRequest(f"{self.name}: {prompt}", prompt))

    def room_path(self, name: str) -> Path:
        room = self.st.data_room
        if room is None:   # Hermie.run creates the room whenever connectors exist; this only guards misuse
            raise RuntimeError("no data room for this task")
        self.st.connector_calls += 1
        safe = re.sub(r"[^A-Za-z0-9._-]", "-", name).replace("..", "__")
        return room / f"{self.st.connector_calls:03d}-{safe}"

    def state(self) -> dict:
        if self._state is None:
            store = self.st.session.connector_states
            self._state = store.load(self.name) if store is not None else {}
            self._loaded = copy.deepcopy(self._state)
        return self._state

    def session_state(self) -> dict:
        return self.st.session.connector_session.setdefault(self.name, {})

    def flush(self) -> None:
        store = self.st.session.connector_states
        if self._state is None or store is None:
            return
        # Only keys this call changed or added: another instance may have saved other keys meanwhile.
        changed = {k: v for k, v in self._state.items() if k not in self._loaded or self._loaded[k] != v}
        if changed:
            store.save(self.name, changed)


def _render(res: ConnectorResult) -> str:
    if not res.files:
        return res.preview
    return (res.preview + "\n\nFull output saved (read-only; compute numbers from it with python in run_command): "
            + ", ".join(str(p) for p in res.files))


async def run_connector_tool(st: "TaskState", connector: Connector, tool: ConnectorTool, args: dict) -> str:
    st.mark_business("connector")      # before the call: the data never exists in an unflagged task
    guard = ConnectorGuard(st, connector.name)
    t0 = time.monotonic()
    res = ConnectorResult(f"{tool.name} was cancelled", ok=False)
    try:
        try:
            res = await tool.call(args, guard)
        except Exception as e:
            # type only: the message may carry business data (asc stderr)
            log.error("Connector %s.%s failed: %s", connector.name, tool.name, type(e).__name__)
            res = ConnectorResult(f"{tool.name} failed: {type(e).__name__}: {e}", ok=False)
    finally:   # also on cancellation: state chosen before the cancel is kept and the call is logged
        try:
            guard.flush()
        except Exception as e:
            log.error("Saving connector state failed: %s", type(e).__name__)
        try:
            if st.session.connector_log is not None:
                st.session.connector_log.write({"connector": connector.name, "tool": tool.name, "label": res.label,
                                                "ok": res.ok, "duration_s": round(time.monotonic() - t0, 2),
                                                "files": [p.name for p in res.files]})
        except Exception as e:
            log.error("Writing connector log failed: %s", type(e).__name__)
    return _render(res)
