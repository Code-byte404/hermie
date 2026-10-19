# End-to-end canary and utility run

CredData measures how well the rules find secrets in source code. This suite asks the two questions a user has
when they hand Claude Code a whole, unsorted work folder:

1. Does anything private leave the machine?
2. Does Claude still answer correctly when Hermie has replaced names, emails, IDs and keys with placeholders?

Design: `docs/superpowers/specs/2026-10-18-e2e-canary-utility-design.md` (local).

## What runs

- `workspace.py` builds a seeded folder: an English sales agreement, a Chinese lease, an employee table, Chinese
  meeting notes with a pasted API key, an English board memo, client and vCard contacts, a forwarded mail with a
  token and a password, and a small Python app (one bug, `.env`, a log). Every planted value is a canary listed in
  `canaries.json` with its kind and whether Hermie claims to protect that kind today.
- `tasks.py` holds 21 tasks (10 with Chinese prompts): contract questions, party separation (buyer vs seller,
  landlord vs tenant), messages to contacts, the top salaries, the meeting decision, a mail reply, the log, the bug
  fix, a config question, one multi-turn task. Each has canaries the answer must and must not mention and an
  English rubric for the judge.
- `run.py` runs every task twice: through Hermie and directly. Each run gets a fresh copy of the folder (without
  `canaries.json`), a fresh **recorder** (`recorder.py`, a forwarding proxy that writes every request body, never
  headers) in front of api.anthropic.com, and for the Hermie arm a fresh Hermie process and data dir (enforce,
  judge off, `en` + `zh`, no prompter). Hermie's `/anthropic` route is pointed at the recorder with the config field
  `anthropic_upstream`. The recorder is the independent witness: Hermie's own `outbound/` is only cross-checked.
- `score.py` searches every recorded body for every canary (`leaks.py`: exact, URL-encoded, normalized spacing,
  12-character key fragments; JSON is parsed, so escaped Chinese is found), checks that each Hermie-arm body
  matches a body in Hermie's `outbound/` (otherwise the run is invalid), and grades answers with script checks and
  a blind judge (`claude -p`, cached).

## What counts

- **Leak:** a canary in a body the Hermie arm sent. All kinds count, including the ones Hermie does not claim today
  (company names, amounts, dates, project names, confidential passages): those are expected to leak and their
  counts are the product gap list.
- A canary is `leaked`, `protected` (only the direct arm sent it) or `not exercised` (no run sent it).
- **Gate 1:** no claimed canary leaked (person, email, phone, national ID, address, bank card, IBAN, API key,
  password, URL credentials).
- **Gate 2:** no task where the direct arm passes and the Hermie arm fails, except noise (repeats disagree).
  Causes: `blocked` (a held user message, 422), `withheld` (a held tool result), `unrestored` (a placeholder left
  in the answer), `swapped` (a must-not value in the answer), `damage` (anything else).

## Key canaries use real provider shapes

`sk-ant-api03-`, `AKIA`, `ghp_`, `xoxb-` and generic high-entropy values, with random bodies. This departs from the
repo's fake-data rule on purpose and only here (approved 2026-10-18): the repo holds the generator (prefixes only),
values exist only in the generated folder outside the repo. gitleaks and detect-secrets scan the folder as an
independent check (`peers` table in the summary). Canaries are frozen once built; they are never tuned to what
Hermie catches.

## Commands

```bash
B=~/hermie-bench.noindex/e2e; R=$B/results/$(date +%F)
conda run -n hermie python -m evals.e2e.workspace --seed 1 --out $B/workspace-check   # look at the folder, offline
conda run -n hermie python -m evals.e2e.run --out $R          # real Claude Code sessions: about 42, ask first
conda run -n hermie python -m evals.e2e.score --out $R        # summary.md, leaks.md, quality.md, reruns.txt
conda run -n hermie python -m evals.e2e.run --out $R --tasks $(cat $R/reruns.txt) --rep 2,3   # then score again
```

The run uses the Claude Code login of whoever starts it and reaches the real Anthropic API. Results stay outside
the repo. Offline tests: `tests/test_e2e.py` (a fake `claude` and a fake upstream).

## Biases and limits

- About 20 tasks, one seed, one model: it finds failure kinds, not precise rates.
- Grading leans on values Hermie replaces; the judge is Claude itself.
- Headless: nobody answers held items, so a held message is blocked or withheld. With the judge off, holds are rare.
- Model output that restates a value is not a leak (the provider only saw placeholders).
