"""Slash-command table: the completion popup and /help are both generated from here, so it is maintained in one place."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


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
    Command("/data", "/data QUESTION", "Ask about your App Store / Apple Ads data; runs locally, nothing goes to the cloud", True),
    Command("/apps", "/apps [NAME]", "List your App Store apps / set the default app for this session", True),
    Command("/ga4", "/ga4 [NAME]", "List your GA4 properties / set the default property for this session", True),
    Command("/new", "/new", "Start a new session: clear executor history and lift the business-data lock"),
    Command("/model", "/model [list | local|judge|cloud|plan NAME]", "Show / switch local and remote models; changes are written to .env", True),
    Command("/voice", "/voice [on|off|list|test|key KEY|VOICE]", "Speech output toggle, voice, record key; changes are written to .env", True),
    Command("/outbound", "/outbound", "Show the outbound log (the exact text sent to the cloud model each time)"),
    Command("/rollback", "/rollback [SNAPSHOT_ID]", "Roll back to a pre-task snapshot (default: the latest one)", True),
    Command("/snapshots", "/snapshots [prune N]", "List snapshots / keep only the latest N", True),
    Command("/perf", "/perf", "Show performance monitoring (CPU / GPU / memory usage graphs)"),
    Command("/usage", "/usage", "Show token usage"),
    Command("/skills", "/skills [approve|retire|restore|open ID]", "List learned skills (playbooks) or change their status", True),
    Command("/calibrate", "/calibrate", "Propose routing thresholds from your recorded tasks (apply with hermie --calibrate --apply)"),
    Command("/export", "/export [PATH]", "Export this session (contains original text; handle with care)", True),
    Command("/clear", "/clear", "Clear the chat area"),
]

_BY_NAME = {c.name: c for c in COMMANDS}


def find_command(name: str) -> Command | None:
    return _BY_NAME.get(name)


def is_command(text: str) -> bool:
    """Whether a submitted line is a slash command rather than a task that starts with a path (a directory dropped
    into an empty input box arrives as "/Users/..."). A known command name always wins; otherwise a first token with
    a further "/", or one naming an existing path, is a path. What is left ("/hlep") is an unknown command."""
    text = text.lstrip()
    if not text.startswith("/"):
        return False
    head = text.split(maxsplit=1)[0]
    if find_command(head):
        return True
    if "/" in head[1:]:
        return False
    try:
        return not Path(head).exists()
    except (OSError, ValueError):
        return True


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
