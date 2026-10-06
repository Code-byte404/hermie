# Architecture

Hermie is one Starlette app (`hermie/proxy/server.py`) with a single route, `/{provider}/{rest:path}`, and a privacy gate (`hermie/gate/`) behind it. This page follows one request through it.

## Request path

1. **Route.** `provider` picks the upstream from `ROUTES`: `anthropic` -> api.anthropic.com, `openai` -> api.openai.com, `gemini` -> generativelanguage.googleapis.com, `custom` -> `custom_upstream` (404 when unset). The upstream URL is the base plus the rest of the path and the query string, as the client encoded them.
2. **Refusals.** Paths with `.` / `..` segments or encoded slashes: 400. `/openai/v1/files`: 415. Model listings (`PASSTHROUGH_PREFIXES`) pass only as body-less GET / DELETE, sent with `empty_body()`; everything else must be a JSON object (415 / 400 otherwise).
3. **Walk.** `walker.walk_request` visits every string leaf of a copy of the body, skipping `SKIP_KEYS` outside tool payloads; inside tool payloads it also scans dict keys and numbers with six or more digits. `classify` labels each leaf by author: `user` (user / system / developer messages, system instructions), `tool` (tool results, function responses), `assistant`, `binary` (image data, never scanned) or `other` (tool definitions and metadata).
4. **Scan.** `Gate.scan` (`gate/gate.py`) runs the Presidio analyzer (`gate/recognizers.py`: secret patterns, Presidio recognizers, spaCy NER, `plausible` filters) over 48 KB chunks, adds every literal occurrence of a value already in the mapping, and replaces findings with `<ENTITY_N>` placeholders (`gate/redact.py`). New placeholders are merged into `mapping.json` under an flock, so two Hermie processes never mint the same name for different values; the file also keeps per-entity counters and a generation, so `hermie forget` never lets a number be reused and a running gate drops its cache. With a judge, `user` and `tool` leaves are also checked for encoded data (`smuggling_risk_text`, or `smuggling_risk_url` for a leaf that is a URL) and then asked to the Ollama judge (`gate/judge.py`). Results are cached by text hash (`cache_mb`), so a conversation's history is scanned once.
5. **Decide.** Per leaf: placeholders only; a flagged `user` leaf is **held** (the walk stops and `approvals.TtyPrompter` asks; reject means HTTP 422); a flagged leaf of any other origin is **withheld** (replaced by `WITHHELD_NOTE`); images are withheld unless `images = "pass"`. Items released with `hermie allow ID` (`allowed.jsonl`, full hashes; ids come from `AllowStore.assign`) or "allow everything this session" (judge and smuggling decisions only) pass. A judge flag on an `other` leaf is ignored; a detector error is not, and it is never released.
6. **Send.** The rewritten body goes through `certify_body` into a `CleanBody`, is stored as `outbound/<id>.json` (unless `bodies = false`; pruned to `bodies_keep_mb`), and `send_upstream` sends it with the allowlisted headers (`FORWARD_HEADERS`). In `observe` mode the original body is sent and stored.

## Response path

- **JSON replies:** every string leaf goes through `Gate.restore`, which swaps placeholders back from the mapping (`stream.restore_json`).
- **Streams (`text/event-stream`):** `stream.relay_sse` parses each event and restores its data. Text under the streamed keys (`BUFFERED_KEYS`: `text`, `partial_json`, `content`, `arguments`, `delta`) passes through a `PlaceholderBuffer`, which holds back a tail that may be the start of a placeholder until the next event completes it. A held tail is flushed back into the leaf that carried it as soon as a leaf at another path is fed (in the same event or a later one), so the client sees the same events; only when no such event is left does Hermie add one (`hermie_flush`).
- Placeholder-shaped tokens that are not in the mapping are left as they are and counted as `unrestored` in the receipt.
- Response headers passed back: request ids, `retry-after`, rate-limit headers, plus `x-hermie-request-id`.

Every request, refused or not, ends with one line in `receipt.jsonl` (`receipt.ReceiptLine`), written when the response is complete (for streams: when the stream ends or the client leaves).

## Data files

All under `data_dir` (default `~/.hermie`), created 0600:

| File | Holds | Written by |
|---|---|---|
| `config.toml` | optional config, see `config.example.toml` | you |
| `mapping.json` | placeholder -> real value; the most sensitive file | `redact.MappingStore` |
| `receipt.jsonl` | one data-free line per request | `receipt.Receipt` |
| `outbound/<id>.json` | each request body exactly as sent | `receipt.BodyStore` |
| `pending.jsonl` | held / withheld items (hash, id, kind, reason, size; no text), last 500 | `approvals.AllowStore` |
| `allowed.jsonl` | hashes released with `hermie allow` or the prompt | `approvals.AllowStore` |

`hermie tail` / `stats` read the receipt, `hermie show` reads `outbound/`, `hermie allow` appends to `allowed.jsonl` (picked up by a running proxy on the next request), `hermie forget` clears the values in `mapping.json` (counters kept, generation bumped).
