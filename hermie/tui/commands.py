"""Slash-command table: the completion popup and /help are both generated from here, so it is maintained in one place."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Command:
    name: str        # e.g. "/model"
    usage: str       # e.g. "/model [list | local|judge|cloud|plan NAME]"
    help: str
    takes_args: bool = False   # True: completion leaves a trailing space for the argument instead of running immediately


COMMANDS: list[Command] = [
    Command("/help", "/help", "Show all commands and shortcuts"),
    Command("/mode", "/mode default|auto", "Switch between default mode / auto mode (or press F2)", True),
    Command("/local", "/local TASK", "Force local execution", True),
    Command("/cloud", "/cloud TASK", "Force cloud (still goes through the privacy gate)", True),
    Command("/model", "/model [list | local|judge|cloud|plan NAME]", "Show / switch local and remote models; changes are written to .env", True),
    Command("/voice", "/voice [on|off|list|test|key KEY|VOICE]", "Speech output toggle, voice, record key; changes are written to .env", True),
    Command("/outbound", "/outbound", "Show the outbound log (the exact text sent to DeepSeek each time)"),
    Command("/rollback", "/rollback [SNAPSHOT_ID]", "Roll back to a pre-task snapshot (default: the latest one)", True),
    Command("/snapshots", "/snapshots [prune N]", "List snapshots / keep only the latest N", True),
    Command("/perf", "/perf", "Show performance monitoring (CPU / GPU / memory usage graphs)"),
    Command("/usage", "/usage", "Show token usage"),
    Command("/export", "/export [PATH]", "Export this session (contains original text; handle with care)", True),
    Command("/clear", "/clear", "Clear the chat area"),
]

_BY_NAME = {c.name: c for c in COMMANDS}


def find_command(name: str) -> Command | None:
    return _BY_NAME.get(name)


def filter_commands(text: str) -> list[Command]:
    """Filter by the input box contents: prefix match while only part of a command is typed; once an argument has
    been started, show just that command."""
    text = text.lstrip()
    if not text.startswith("/") or "\n" in text:
        return []
    head, sep, _ = text.partition(" ")
    if sep:
        c = find_command(head)
        return [c] if c else []
    return [c for c in COMMANDS if c.name.startswith(head)]


def help_markdown(record_key: str) -> str:
    rows = "\n".join(f"| `{c.usage}` | {c.help} |" for c in COMMANDS)
    return (
        "**Slash commands** (typing `/` opens the candidate list; up/down to select, Tab to complete, Enter to run)\n\n"
        "| Command | Effect |\n| --- | --- |\n" + rows +
        f"\n\nEnter send · Ctrl+J newline · Esc interrupt the current task (cancels the recording while recording) · F2 switch mode · "
        f"{record_key.upper()} record/stop (the transcript is inserted into the input box) · F6 speech output toggle · "
        "Ctrl+B / Ctrl+R collapse the left / right pane · Ctrl+Q quit\n\nNote: the terminal scrollback also contains the executor's output."
    )
