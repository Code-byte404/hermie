# Security and privacy

Hermie sits between a coding agent and its cloud API and replaces private values in what the agent sends. This page says exactly what that covers, what it does not cover, and how to report a hole in it.

## What is guaranteed

- **Fail closed.** Any exception in the detectors (Presidio, spaCy) or the judge marks the text sensitive: a user message is held for approval, anything else is withheld and replaced by a note. If the placeholder mapping cannot be written, the request is refused with HTTP 507 and nothing is sent.
- **One typed sender.** `send_upstream` (`hermie/proxy/server.py`) is the only function that sends a request upstream, and it only accepts a `CleanBody`. A `CleanBody` is built only by `gate.certify_body` (after the walk has redacted the body) or `gate.empty_body` (body-less passthrough requests).
- **Every string is scanned.** Every string leaf of the JSON body is scanned unless its key is in `SKIP_KEYS` (`hermie/proxy/walker.py`: protocol fields such as `model`, `role`, `type`, `id`). `SKIP_KEYS` never apply inside tool payloads (tool results, tool-call inputs and arguments, function responses), so a tool output with an `id` or `name` field is scanned like any other text. The exceptions are the ones you configure: `allow_values` and `allow_paths`.
- **The receipt holds no values.** `receipt.jsonl` stores per request: time, request id, client label, upstream, model, sizes, entity names and counts, withheld / held ids, judge call counts, status and an exception class name. `ReceiptLine` has no free-text field, and the writer drops anything else a caller puts in.
- **Private files.** `mapping.json`, `receipt.jsonl`, the stored bodies in `outbound/`, `allowed.jsonl` and `pending.jsonl` are created with mode 0600.
- **No key is stored.** There is no key in the config. API keys and login tokens come from the client's headers, are forwarded to the upstream and are never written to disk; `outbound/` stores bodies, not headers.
- **Auth headers go only to their own upstream.** Headers on the forwarding allowlist (`FORWARD_HEADERS`) are sent only to the upstream of the route the client called (`/anthropic`, `/openai`, `/gemini`, or the configured `/custom` upstream). Everything else in the request headers is dropped.

## What is not guaranteed

- **Detection above the rules layer is probabilistic.** Regex recognizers (keys, phones, cards, emails) are deterministic; spaCy NER and the judge are not. Without `--judge`, only the rules and Presidio run. Use `deny_words` for names and codenames that matter to you.
- **Inference.** The model can still infer that a person, a phone number or a key exists from the placeholder and the text around it.
- **Your code is sent.** The upstream still sees your source code. Hermie protects data in the code and the conversation, not the code itself.
- **Requests that bypass the base URL are invisible.** Telemetry, OAuth login flows, Gemini CLI's Code Assist endpoint (used with Google login), and any client that ignores the base URL variable do not go through Hermie.
- **`observe` mode blocks nothing.** It sends every request unchanged, so `outbound/` then holds the real values too.
- **Very large text.** A single text leaf beyond spaCy's limit (1,000,000 characters) cannot be scanned, so it is withheld (or held, for a user message) rather than redacted.
- **The query string is forwarded unscanned.** Only the JSON body is scanned.
- **Narrow passthrough.** Requests without a JSON body pass only as body-less GET / DELETE on the model listing paths (`PASSTHROUGH_PREFIXES`). Anything else that is not JSON is refused with 415, and file uploads (`/openai/v1/files`) are refused with 415. Paths with `.` or `..` segments or encoded slashes are refused with 400.
- **The mapping file.** `~/.hermie/mapping.json` maps every placeholder back to its real value. It is the most sensitive file on the machine: mode 0600, never sent anywhere, and `hermie forget` clears it. Anyone who can read your home directory can read it.
- **Local attackers.** Hermie listens on `127.0.0.1` by default and has no authentication; any local process can use it. The threat model is data leaving the machine, not other software on it.

## Reporting a vulnerability

If you find a way for a value Hermie should have replaced to reach the upstream, please do not open a public issue. Use GitHub's private vulnerability reporting on this repository ("Report a vulnerability" under the Security tab). Include:

- the receipt line of the request (`hermie tail --json --since 1h`),
- the request id, which is also the name of the stored body in `~/.hermie/outbound/<id>.json` (share the body only if it contains nothing real),
- how to reproduce it with fake data.

Never send `~/.hermie/mapping.json`. It holds the real values.

Bypasses of the gate are treated as the highest severity. A false positive (clean text replaced or held) is a quality bug and can go in a normal issue with an eval case attached.
