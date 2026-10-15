# Hermie menu for Claude Code

A Claude Code mod (needs a build with mods, tested on 2.1.295). It does two things:

- When `hermie serve` holds a message, a bordered menu appears above the prompt instead of a macOS dialog. It says why the message was stopped, shows its opening (with placeholders) and the real values already replaced, and lists each choice with what it does: 1. Don't send (the prompt fails with an error; nothing leaves), 2. Send this one (not asked about again), 3. Send all, this session (judge and encoded-data holds go out until `hermie serve` restarts; replacing still applies), and, with `local_model` set in Hermie's config, 4. Rewrite locally. A countdown shows when an unanswered message counts as Don't send. Don't send has the focus; the number keys answer directly. "Rewrite locally" has the local model find the specifics (names, figures, places, non-public facts); they become placeholders, and a second screen shows the result: 1. Don't send, 2. Redo, 3. Send this. Only a version you send leaves the machine, and the reply comes back with the real values restored.
- A one-line band shows the last reply: how many values were replaced (with the values), what was withheld or held and the `hermie allow` command for it.

It polls `hermie asks --json` once a second and answers with `hermie answer ID send|reject|session|local|again`; the band reads `hermie status --json`. All wording comes from those commands (`hermie/explain.py`), so the menu, the terminal prompt, the dialog and the turn summary say the same thing. The ask file holds the opening of the held message only while it waits for your answer. Without this mod (or when it is not polling) `hermie serve` falls back to the macOS dialog.

```bash
claude --plugin-dir /path/to/hermie/integrations/claude-code-menu
```

Run `hermie serve` without a terminal (for example `hermie serve --judge ... < /dev/null`): in a terminal it asks there instead. If `hermie` is not on Claude Code's PATH, set `HERMIE` at the top of `hooks/register.tsx` to its full path.
