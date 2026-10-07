<p align="center">
  <img src="assets/logo.png" width="160" alt="Hermie, a hermit crab peeking out of its shell">
</p>

<h1 align="center">Hermie</h1>

<p align="center">Hermie is a local proxy between a coding agent and its cloud API: it redacts what goes out, restores placeholders in what comes back, and keeps a verbatim receipt of every request.</p>

<p align="center">
  <a href="LICENSE"><img alt="MIT" src="https://img.shields.io/badge/license-MIT-orange"></a>
  <img alt="Python 3.12" src="https://img.shields.io/badge/python-3.12-blue">
  <img alt="platform: macOS / Linux" src="https://img.shields.io/badge/platform-macOS%20%2F%20Linux-lightgrey">
</p>

## What it looks like

![A Claude Code prompt containing a customer's name, phone, email and a key. Claude answers normally. hermie show prints the user message Anthropic actually received: the four values are placeholders](assets/demo.gif)

A real run (Claude Code 2.1.291 through `hermie serve`, 2026-10-07). You type a normal prompt with a customer's details in it:

```text
$ claude -p "Our customer Maria Gonzalez (555-010-0199, maria.gonzalez@example.com) says her
  Stripe key sk-test-hermie-demo-not-a-real-key-0000 stopped working. What should I check first?"
```

Claude answers as usual. `hermie show ID` prints the stored request body exactly as it was sent; the user message in it reads:

```text
Our customer <PERSON_2> (<PHONE_NUMBER_1>, <EMAIL_ADDRESS_3>) says her Stripe key <SECRET_1>
stopped working. What should I check first?
```

and the receipt line for that request (`hermie tail`) shows what was replaced, never the values:

```text
  [user]  -  189  EMAIL_ADDRESS, PERSON, PHONE_NUMBER, SECRET
```

The numbering continues from earlier placeholders in the same request (Claude Code's system prompt carries your `CLAUDE.md`, git status and the email in your git config, which Hermie scans too). The reply comes back with the real values restored, so a tool call like `grep <PHONE_NUMBER_1> customers.csv` runs with the actual number while the model only ever saw the placeholder. `demo/record-prompt.sh` is the script behind the recording (`asciinema` + `agg`); `demo/record.sh` opens the agent and `hermie tail` side by side in tmux for a longer session.

## Quickstart

```bash
pip install git+https://github.com/Code-byte404/hermie && python -m spacy download en_core_web_lg
hermie serve                                   # prints the base URLs
ANTHROPIC_BASE_URL=http://127.0.0.1:8787/anthropic claude
hermie tail                                    # in another terminal
```

After 0.3.0 is published on PyPI, the first line becomes `pip install hermie && python -m spacy download en_core_web_lg`.

`hermie serve` listens on `127.0.0.1:8787` and prints one base URL per upstream: `/anthropic` (api.anthropic.com), `/openai/v1` (api.openai.com), `/gemini` (generativelanguage.googleapis.com) and `/custom` (any upstream you name with `--upstream`). Your API key or login token is forwarded to the upstream as the client sends it; Hermie never stores it.

Other commands: `hermie show ID` (the stored outbound body of a request), `hermie allow ID` (release a held or withheld item), `hermie stats [--days N]` (totals from the receipt), `hermie forget` (clear the placeholder mapping). `hermie COMMAND --help` lists the options.

## What leaves your machine

Every string in the JSON request body is scanned before the request is sent (inside tool payloads also dict keys and long numbers; long texts in 48 KB chunks):

1. **Rules.** Regex recognizers for API keys and credentials (OpenAI, Anthropic, AWS, GitHub, Slack, Google, Stripe, Hugging Face, npm, PyPI, private-key blocks, JWTs, database URLs, `password=...` assignments), phone numbers, emails, cards, IBANs, IP addresses and US SSN / passport / driver-license numbers. Matches become placeholders such as `<PHONE_NUMBER_1>`.
2. **NER.** Presidio with spaCy finds person names (two or more capitalized words), also replaced by placeholders. Optional Chinese engine (`languages = ["en", "zh"]`) adds mainland mobile, ID-card and bank-card numbers.
3. **Judge (optional, `--judge ollama:MODEL`).** A local Ollama model answers "is this sensitive?" for user messages and tool results that the first two layers left alone, and flags encoded data (high-entropy base64 tokens; in URLs also hex tokens, long query strings and digit runs). It sees the first 8000 characters of a text. A flagged user message is held for you; a flagged tool result is withheld and replaced by a short note.

Images are withheld by default (`images = "pass"` sends them). The same value always gets the same placeholder, also where it comes back in the agent's own history (assistant text, tool calls, tool results), and replies are restored from a local mapping before the agent sees them, in JSON replies and in streams (a placeholder split across two stream events is reassembled). `hermie forget` deletes the stored values; placeholder numbers are never reused, so an old placeholder never restores to a different value.

## Verified clients

| Client | How to point it at Hermie | Status |
|---|---|---|
| Claude Code 2.1.291 | `ANTHROPIC_BASE_URL=http://127.0.0.1:8787/anthropic` | Verified with a subscription login (2026-10-06): no real value reached the stored outbound bodies, placeholders were restored, receipt labelled `claude-code`. |
| Codex CLI 0.147.0 | a `model_providers` entry in `~/.codex/config.toml` with `base_url = "http://127.0.0.1:8787/openai/v1"`, `wire_api = "responses"`, `env_key = "OPENAI_API_KEY"` | Not verified. The ChatGPT login does not go through a custom `base_url`; only API-key use through the provider entry can work. |
| Gemini CLI 0.44.1 | `GEMINI_API_KEY=...` and `GOOGLE_GEMINI_BASE_URL=http://127.0.0.1:8787/gemini` | Not verified. Google login (`oauth-personal`) talks to the Code Assist endpoint and bypasses `GOOGLE_GEMINI_BASE_URL`; only API-key auth can work. |
| aider | `ANTHROPIC_BASE_URL=http://127.0.0.1:8787/anthropic` or `OPENAI_BASE_URL=http://127.0.0.1:8787/openai/v1` | Not tested (not installed during the live run). Expected to work through either variable. |

The live tests are in `tests/test_live.py` (`pytest -m live tests/test_live.py::test_claude_code -s`).

## Modes

- `enforce` (default): replace, withhold and hold as described above.
- `observe` (`hermie serve --mode observe`): scan and write the receipt, but send every request unchanged. Nothing is blocked or replaced. Use it to see what Hermie would do before you rely on it.

## When the judge flags something

With a judge configured, a user message the judge flags is held. If `hermie serve` runs in a terminal, it asks there:

```text
Hermie holds this message (412 chars): ...
  reason: judge
  kind: message
  [s]end as is  [r]eject  [a]llow everything this session
  or later: hermie allow 3fa2
```

Type `s`, `r` or `a` and Enter. No answer within `hold_timeout_s` (60 s) rejects. Without a terminal (for example under a process manager) held messages are rejected right away. A rejected request returns HTTP 422 to the agent with a message that names the id; run `hermie allow ID` in any terminal and retry. Withheld tool results and images show up in `hermie tail` with the same `hermie allow ID` line; once allowed, the next request sends them. `[a]llow everything this session` covers judge decisions only. A text the detectors failed on cannot be released at all, and `hermie allow` refuses an id it does not know.

## What this does not protect

- Detection above the rules layer is probabilistic, and without `--judge` only the rules and Presidio run. A name or a secret in an unusual shape can pass.
- The model can still infer that a person, a phone number or a key exists from the placeholder and the surrounding text.
- The upstream still sees your code. Hermie protects data in the code and the conversation, not the code itself.
- Requests that do not go through the base URL are invisible to Hermie: telemetry, OAuth login flows, Gemini's Code Assist endpoint, or any client that ignores the variable.
- `observe` mode blocks nothing.
- Placeholders in replies are restored before the agent runs its tools, so a prompt-injected model can make the agent use a real value locally (for example in a URL it fetches).
- Known values are matched literally (four characters or longer), so a first name alone can pass after only the full name was mapped, and a short mapped value inside a longer word is over-redacted (`remain` when `main` is mapped); the reply restores it.
- In OpenAI chat streams with parallel tool calls, a placeholder tail held at the end of one call's arguments can land in the next call's arguments.

[SECURITY.md](SECURITY.md) has the full list, including the query string, passthrough paths and the mapping file.

## Configuration

Copy [`config.example.toml`](config.example.toml) to `~/.hermie/config.toml`; every field is commented there. Each field can also be set as an environment variable `HERMIE_<FIELD>` (for example `HERMIE_PORT=8788`, `HERMIE_DENY_WORDS=Project Falcon,ACME`), and the `hermie serve` flags (`--host`, `--port`, `--mode`, `--judge`, `--upstream`, `--no-bodies`, `--data-dir`) override both; `--config PATH` picks the file to read. Useful fields: `deny_words` (names and codenames that must always be replaced), `allow_values` (exact values never replaced), `allow_paths` (globs matched against a tool result whose first line is a bare file path; that result is not scanned), `images`, `bodies` / `bodies_keep_mb` (the stored outbound bodies), `hold_timeout_s`.

Everything Hermie writes lives in `~/.hermie`: `receipt.jsonl`, `outbound/`, `mapping.json`, `allowed.jsonl`, `pending.jsonl`. See [docs/architecture.md](docs/architecture.md).

## Roadmap

- Cursor, once its request origin (client or Cursor's servers) is verified.
- Per-project mapping namespaces.
- A judge that does not need Ollama.
- A launchd / systemd unit to run `hermie serve` in the background.
- Faster scanning of tool results with many findings.
- Per-call leaf identity for OpenAI chat parallel tool calls (see the limitation above).
- Windows: the file locks use `fcntl`, which Windows does not have.

## Contributing

- A new recognizer: the pattern, eval cases in `evals/`, and `tests/test_evals.py` green.
- A new client: a request fixture (`tests/fixtures/requests`), a stream fixture (`tests/fixtures/streams`), a `BUFFERED_KEYS` entry in `hermie/proxy/stream.py` for its streamed text keys, and a live test in `tests/test_live.py`.

Details in [CONTRIBUTING.md](CONTRIBUTING.md). Security reports go through [SECURITY.md](SECURITY.md).

## License

MIT, see [LICENSE](LICENSE).

Hermie used to be a local-first coding agent; that project is archived at [hermie-agent](https://github.com/Code-byte404/hermie-agent).
