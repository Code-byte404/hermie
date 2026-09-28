"""Slash-command table: completion filtering and /help generation."""
from hermie.tui.commands import COMMANDS, filter_commands, find_command, help_markdown


def test_filter_by_prefix_and_args():
    assert [c.name for c in filter_commands("/")] == [c.name for c in COMMANDS]
    assert [c.name for c in filter_commands("/mo")] == ["/mode", "/model"]
    assert [c.name for c in filter_commands("/model lo")] == ["/model"]     # while typing an argument only that command shows
    assert filter_commands("/nope") == [] and filter_commands("hello") == [] and filter_commands("/mo\nx") == []
    assert find_command("/voice").takes_args and not find_command("/clear").takes_args


def test_help_lists_every_command_and_record_key():
    text = help_markdown("f8")
    assert all(c.usage in text for c in COMMANDS) and "F8 record" in text
