# Hermie menu for Claude Code

A Claude Code mod (needs a build with mods, tested on 2.1.295). It does two things:

- When `hermie serve` holds a message, a bordered menu appears above the prompt (1. Reject, 2. Send as is, 3. Allow session) instead of a macOS dialog. Reject has the focus; keys 1, 2, 3 answer directly.
- A one-line band shows how many values the last request replaced and whether anything is withheld.

It polls `hermie asks --json` once a second and answers with `hermie answer ID send|reject|session`. It shows ids, sizes and reasons only, never the held text. Without this mod (or when it is not polling) `hermie serve` falls back to the macOS dialog.

```bash
claude --plugin-dir /path/to/hermie/integrations/claude-code-menu
```

Run `hermie serve` without a terminal (for example `hermie serve --judge ... < /dev/null`): in a terminal it asks there instead. If `hermie` is not on Claude Code's PATH, set `HERMIE` at the top of `hooks/register.tsx` to its full path.
