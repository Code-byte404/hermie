"""Claude Code integration: the status line (`hermie status`), hook events (`hermie hook`) and their installation
(`hermie install-hooks`). Everything here reads the receipt and the mapping file; nothing here sees request text.
Real values come from the local mapping and are shown only to the person: a `systemMessage` and the status line
never reach the model. The wording lives in `explain`.

Claude Code calls `hermie hook` with one JSON object on stdin per event. `UserPromptSubmit` stores a turn marker
(the time, under the session id); `Stop` sums the receipt lines since that marker into one `systemMessage`;
`PostToolUse` for a file-writing tool reports placeholders restored in the reply that produced the write. A
`systemMessage` is shown to the user and never added to the model's context; an empty stdout means nothing to say.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hermie import explain
from hermie.config import Config
from hermie.gate.redact import MappingStore, MappingStoreError
from hermie.proxy.receipt import Receipt, ReceiptLine

FILE_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
_SESSION = re.compile(r"[A-Za-z0-9_-]{1,80}")
_STAMP = "%Y-%m-%dT%H:%M:%SZ"
_FALLBACK_WINDOW = timedelta(minutes=5)   # a Stop without a marker (hook installed mid-session) looks this far back


# --- status ---

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_stamp(at: str) -> datetime | None:
    try:
        return datetime.strptime(at, _STAMP).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def write_serve_file(config: Config) -> None:
    """`hermie serve` announces itself here; a probe over HTTP would leave a receipt line per status refresh."""
    path = config.serve_path
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": os.getpid(), "host": config.host, "port": config.port,
                                    "started": _now().strftime(_STAMP)}))
    except OSError:
        pass


def remove_serve_file(config: Config) -> None:
    path = config.serve_path
    try:
        if path is not None and json.loads(path.read_text()).get("pid") == os.getpid():
            path.unlink()
    except (OSError, ValueError, AttributeError):
        pass


def serving(config: Config, timeout_s: float = 0.3) -> dict | None:
    """The running proxy's {pid, host, port, started}, or None: no serve file, its process is gone, or nothing
    accepts connections on its port (a stale file after a crash is ignored, not trusted)."""
    try:
        info = json.loads(config.serve_path.read_text())
        pid, host, port = int(info["pid"]), str(info["host"]), int(info["port"])
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass   # alive, owned by someone else
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return {"pid": pid, "host": host, "port": port, "started": info.get("started")}
    except OSError:
        return None


def kept_values(config: Config) -> int | None:
    """How many values the mapping holds, None when the file cannot be read."""
    try:
        return len(MappingStore(config.mapping_path).mapping)
    except MappingStoreError:
        return None


def _last_line(config: Config) -> ReceiptLine | None:
    last = None
    for line in Receipt(config).iter(since=_now() - timedelta(days=1)):
        last = line
    return last


def _mapping(config: Config) -> dict:
    try:
        return MappingStore(config.mapping_path).mapping
    except MappingStoreError:
        return {}


def status_json(config: Config) -> dict:
    last = _last_line(config)
    up = serving(config)
    hidden = kept_values(config)
    return {"running": up is not None, "serve": up, "kept": hidden, "hidden": hidden,
            "last": None if last is None else last.__dict__,
            "last_text": None if last is None else explain.last_text(last, _mapping(config))}


def status_text(config: Config) -> str:
    """One line for a status bar: whether the proxy answers, how many values are hidden, the last request."""
    up = serving(config)
    kept = kept_values(config)
    kept_text = "mapping unreadable" if kept is None else f"{kept} value{'s' if kept != 1 else ''} hidden so far"
    state = f":{up['port']}" if up else f"not running (expected :{config.port})"
    head = f"hermie {'●' if up else '○'} {state} · {kept_text}"
    last = _last_line(config)
    if last is None:
        return head
    return f"{head} · last: {explain.last_text(last, _mapping(config))}"


# --- hooks ---

def _marker_path(config: Config, session_id) -> Path | None:
    if not isinstance(session_id, str) or not _SESSION.fullmatch(session_id) or config.turns_dir is None:
        return None
    return config.turns_dir / f"{session_id}.json"


def _mark_turn(config: Config, session_id) -> None:
    path = _marker_path(config, session_id)
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"at": _now().strftime(_STAMP)}))


def _turn_start(config: Config, session_id) -> datetime:
    path = _marker_path(config, session_id)
    try:
        at = _parse_stamp(json.loads(path.read_text()).get("at")) if path is not None else None
    except (OSError, ValueError, AttributeError):
        at = None
    return at if at is not None else _now() - _FALLBACK_WINDOW


def _turn_lines(config: Config, session_id) -> list[ReceiptLine]:
    return list(Receipt(config).iter(since=_turn_start(config, session_id)))


def _stop_message(config: Config, event: dict) -> str | None:
    return explain.turn_summary(_turn_lines(config, event.get("session_id")), _mapping(config))


def _post_tool_message(config: Config, event: dict) -> str | None:
    tool = event.get("tool_name")
    if tool not in FILE_TOOLS:
        return None
    restored = [ln for ln in _turn_lines(config, event.get("session_id")) if ln.restored]
    if not restored:
        return None
    n = restored[-1].restored
    target = (event.get("tool_input") or {}).get("file_path") if isinstance(event.get("tool_input"), dict) else None
    where = f" ({Path(str(target)).name})" if target else ""
    return explain.post_tool_text(n, tool, where)


def handle_hook(event: dict, config: Config) -> dict | None:
    """The JSON Claude Code should get back for `event`, or None for an empty stdout. Never raises."""
    try:
        name = event.get("hook_event_name") if isinstance(event, dict) else None
        if name == "UserPromptSubmit":
            _mark_turn(config, event.get("session_id"))
            return None
        if name == "Stop":
            msg = _stop_message(config, event)
        elif name == "PostToolUse":
            msg = _post_tool_message(config, event)
        else:
            msg = None
        return {"systemMessage": msg} if msg else None
    except Exception:   # a hook must never break the agent; there is nothing to report but a missing line
        return None


# --- installation ---

HOOK_EVENTS = {"UserPromptSubmit": None, "PostToolUse": "|".join(FILE_TOOLS), "Stop": None}


def _is_ours(command) -> bool:
    """`<hermie> hook|status [--data-dir DIR]`, where <hermie> is a `hermie` executable or `<python> -m hermie.cli`
    (shell-quoted, as `install` writes it; older unquoted entries parse the same)."""
    if not isinstance(command, str):
        return False
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    for i in range(len(argv) - 1):
        if argv[i] == "--data-dir":
            del argv[i:i + 2]
            break
    if len(argv) < 2 or argv[-1] not in ("hook", "status"):
        return False
    prog = argv[:-1]
    return ((len(prog) == 1 and os.path.basename(prog[0]) == "hermie")
            or (len(prog) == 3 and prog[1:] == ["-m", "hermie.cli"]))


def _entry_is_ours(entry) -> bool:
    hooks = entry.get("hooks") if isinstance(entry, dict) else None
    return bool(hooks) and all(isinstance(h, dict) and _is_ours(h.get("command")) for h in hooks)


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text("utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not hold a JSON object")
    return data


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", "utf-8")


def _strip_ours(data: dict) -> dict:
    hooks = data.get("hooks")
    if isinstance(hooks, dict):
        for event in list(hooks):
            entries = hooks[event]
            if isinstance(entries, list):
                entries[:] = [e for e in entries if not _entry_is_ours(e)]
            if not entries:
                del hooks[event]
        if not hooks:
            del data["hooks"]
    sl = data.get("statusLine")
    if isinstance(sl, dict) and _is_ours(sl.get("command")):
        del data["statusLine"]
    return data


def hook_settings(command: str | list[str], data_dir: str | None = None) -> dict:
    """The three hooks and the status line as a Claude Code settings object. `command` is how Claude Code should
    start hermie (an executable, or argv); every part is shell-quoted."""
    command = shlex.join([command] if isinstance(command, str) else command)
    suffix = f" --data-dir {shlex.quote(data_dir)}" if data_dir else ""
    hooks: dict = {}
    for event, matcher in HOOK_EVENTS.items():
        entry: dict = {}
        if matcher:
            entry["matcher"] = matcher
        entry["hooks"] = [{"type": "command", "command": f"{command} hook{suffix}"}]
        hooks[event] = [entry]
    return {"hooks": hooks, "statusLine": {"type": "command", "command": f"{command} status{suffix}"}}


def has_hooks(path: Path) -> bool:
    """Whether the settings file at `path` already runs `hermie hook` (written by `hermie install-hooks`)."""
    try:
        hooks = _load(path).get("hooks")
    except (OSError, ValueError):
        return False
    return isinstance(hooks, dict) and any(
        isinstance(entries, list) and any(_entry_is_ours(e) for e in entries) for entries in hooks.values())


def install(path: Path, command: str | list[str], data_dir: str | None = None) -> list[str]:
    """Add the three hooks and the status line to the settings file at `path` (merged, idempotent). Returns notes."""
    ours = hook_settings(command, data_dir)
    data = _strip_ours(_load(path))
    hooks = data.setdefault("hooks", {})
    for event, entries in ours["hooks"].items():
        hooks.setdefault(event, []).extend(entries)
    notes = []
    status = ours["statusLine"]["command"]
    if "statusLine" in data:
        notes.append(f"kept your status line ({data['statusLine'].get('command')}); "
                     f"call `{status}` from it to show Hermie there too")
    else:
        data["statusLine"] = ours["statusLine"]
    _save(path, data)
    return notes


def uninstall(path: Path) -> None:
    if path.exists():
        _save(path, _strip_ours(_load(path)))
