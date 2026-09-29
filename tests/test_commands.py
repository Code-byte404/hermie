"""Slash-command table: completion filtering and /help generation."""
from hermie.tui.commands import COMMANDS, filter_commands, find_command, help_markdown, is_command


def test_filter_by_prefix_and_args():
    assert [c.name for c in filter_commands("/")] == [c.name for c in COMMANDS]
    assert [c.name for c in filter_commands("/mo")] == ["/mode", "/model"]
    assert [c.name for c in filter_commands("/model lo")] == ["/model"]     # while typing an argument only that command shows
    assert filter_commands("/nope") == [] and filter_commands("hello") == [] and filter_commands("/mo\nx") == []
    assert find_command("/voice").takes_args and not find_command("/clear").takes_args


def test_help_lists_every_command_and_record_key():
    text = help_markdown("f8")
    assert all(c.usage in text for c in COMMANDS) and "F8 record" in text


def test_is_command_tells_commands_from_paths(tmp_path):
    assert is_command("/help") and is_command("/local fix the build") and is_command("  /mode auto")
    assert is_command("/hlep")                          # unknown command: still reported as one
    assert not is_command("fix /etc/hosts")
    assert not is_command("/Users/me/proj/crypto-news")   # a dropped directory, even one that does not exist
    assert not is_command(f"{tmp_path} summarize this")
    assert not is_command("/tmp what is in here")        # single-segment path that exists
