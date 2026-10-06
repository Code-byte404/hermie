# Security and privacy

Hermie sits between a coding agent and its cloud API and replaces private values in what the agent sends. This page says exactly what that covers, what it does not cover, and how to report a hole in it.

## What is guaranteed

- **Fail closed.** Any exception in the judge marks the text sensitive: a user message is held for approval, anything else is withheld and replaced by a note. Any exception in the detectors (Presidio, spaCy) does the same, and such a text is never released: neither `hermie allow` nor "allow everything this session" sends it, because it has no scanned form (a user message is rejected with HTTP 422 saying it could not be scanned). If the placeholder mapping cannot be written, the request is refused with HTTP 507 and nothing is sent.
- **One typed sender.** `send_upstream` (`hermie/proxy/server.py`) is the only function that sends a request upstream, and it only accepts a `CleanBody`. A `CleanBody` is built only by `gate.certify_body` (after the walk has redacted the body) or `gate.empty_body` (body-less passthrough requests).
- **Every string is scanned.** Every string leaf of the JSON body is scanned unless its key is in `SKIP_KEYS` (`hermie/proxy/walker.py`: protocol fields such as `model`, `role`, `type`, `id`), and inside tool payloads also dict keys and numbers with six or more digits (a card number sent as a JSON number, an email used as a key). `SKIP_KEYS` never apply inside tool payloads (tool results, tool-call inputs and arguments, function responses), so a tool output with an `id` or `name` field is scanned like any other text. The exceptions are the ones you configure: `allow_values` and `allow_paths`.
- **Known values stay replaced.** A value that already has a placeholder is replaced wherever it appears literally (four characters or longer), in every leaf including assistant text and tool calls, so the real values the agent got back in a reply map back to the same placeholders on the next turn.
- **A placeholder name is never reused.** `mapping.json` keeps a counter per entity that `hermie forget` does not reset, so an old placeholder can never restore to a different value.
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
- **Very large text.** Texts are scanned in chunks of 48 KB cut at line breaks (spaCy alone refuses texts over 1,000,000 characters). An entity that runs into a chunk's end is scanned again from its start, but a value split across a cut that no recognizer sees in either half can pass.
- **The judge sees only the first 8000 characters of a leaf.** Past that, only the rules, NER and the encoded-data check apply.
- **Restored tool arguments run with real values.** Placeholders in a reply are restored before the agent sees it, including in tool-call arguments the agent then executes. A prompt-injected model can therefore route a placeholder into a command (for example a URL it fetches) and the client runs it with the real value. Hermie guards what is sent to the model, not what the client does with the reply.
- **The query string is forwarded unscanned.** Only the JSON body is scanned.
- **Narrow passthrough.** Requests without a JSON body pass only as body-less GET / DELETE on the model listing paths (`PASSTHROUGH_PREFIXES`). Anything else that is not JSON is refused with 415, and file uploads (`/openai/v1/files`) are refused with 415. Paths with `.` or `..` segments or encoded slashes are refused with 400.
- **The mapping file.** `~/.hermie/mapping.json` maps every placeholder back to its real value. It is the most sensitive file on the machine: mode 0600, never sent anywhere. Anyone who can read your home directory can read it. `hermie forget` deletes the values but keeps the per-entity counters; placeholders in old conversations then stay unrestored, and a running `hermie serve` drops its cache on its next request, so values that come back get new placeholders.
- **Local attackers.** Hermie listens on `127.0.0.1` by default and has no authentication; any local process can use it. The threat model is data leaving the machine, not other software on it.

## Reporting a vulnerability

If you find a way for a value Hermie should have replaced to reach the upstream, please do not open a public issue. Use GitHub's private vulnerability reporting on this repository ("Report a vulnerability" under the Security tab). Include:

- the receipt line of the request (`hermie tail --json --since 1h --once`),
- the request id, which is also the name of the stored body in `~/.hermie/outbound/<id>.json` (share the body only if it contains nothing real),
- how to reproduce it with fake data.

Never send `~/.hermie/mapping.json`. It holds the real values.

Bypasses of the gate are treated as the highest severity. A false positive (clean text replaced or held) is a quality bug and can go in a normal issue with an eval case attached.
