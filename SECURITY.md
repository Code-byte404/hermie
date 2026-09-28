# Security and privacy

Hermie's whole reason to exist is that local material never reaches a cloud model. This page says exactly what that promise covers, what it does not cover, and how to report a hole in it.

## What is guaranteed

- **Fail closed.** Any exception inside the privacy detector (Presidio, spaCy) or the local judge model is treated as "contains private data". Any cloud-model error falls back to local execution, keeping partial local results.
- **Outbound is typed.** The cloud agent only accepts `CleanText`, which can only be constructed by `PrivacyGate.certify()` (rules + NER + judge). `trusted_template()` exists for hard-coded constants only.
- **Outbound guard.** Before every cloud model request, every non-model-generated message part is checked against the certified set; anything unregistered is re-certified on the spot or the request is aborted with `OutboundBlockedError`.
- **Control plane / data plane split.** The cloud planner sees only the de-identified task text, a deterministic workspace overview, the project doc after the gate, and fixed-structure reports. Raw input, file contents, diffs, the executor's `answer` and review problems stay on the machine.
- **Kernel-enforced sandbox.** The executor's file reads, writes and shell commands run under macOS Seatbelt (`sandbox-exec`): write access only to the workspace and the per-user temp/cache dirs, no network, no `~/.ssh`, keychain or browser data, `open` / `osascript` / `security` unavailable, environment reduced to a fixed allowlist. Credential-looking files in the workspace (`id_rsa`, `*.pem`, `*.key`, `.netrc`, see `SANDBOX_DENY_NAMES`) are unreadable even there.
- **Web access is inbound only.** `web_fetch` / `web_search` run in the controller process, never in the sandbox. Every URL and query is outbound content and goes through the gate; after the task has touched sensitive data the gate additionally checks for encoded smuggling (base64, hex, long digit runs, oversized queries) and asks the judge once more.
- **Audit log stores hashes.** `audit.jsonl` records route, signals and counts plus the SHA-256 of the input, never the input. `outbound.jsonl` records exactly what was certified and sent.

## What is not guaranteed

- Detection is probabilistic above the rules layer. Regex recognizers (phones, ID numbers, cards, secrets) are deterministic; NER and the judge model are not. A person name the Chinese spaCy model misses and the judge does not flag can leave. Use `SENSITIVE_KEYWORDS` for names and codenames that matter to you.
- The thresholds shipped in `.env.example` are starting points, not calibrated values. `evals/` has the tooling to calibrate them on your own traffic.
- `RunMode.NO_SANDBOX` (`--dangerously-no-sandbox`) removes the kernel boundary. The remaining path check in `_fsops._resolve` is a courtesy, not a security boundary.
- `.env` in the workspace is deliberately readable by the executor (project build commands need it). Keys inside it are caught at the outbound gate by the secret recognizer, but the executor itself can read them.
- Seatbelt is deprecated by Apple. It still works on current macOS; the sandbox layer is behind an interface (`sandbox.Executor`) so it can be replaced.
- Hermie does not protect against a malicious local model. The executor and judge run on your machine under your control; the threat model is data leaving the machine, not the local model misbehaving.

## Reporting a vulnerability

If you find a way for local data to reach the cloud model, or for the executor to escape the sandbox, please do not open a public issue. Use GitHub's private vulnerability reporting on this repository ("Report a vulnerability" under the Security tab). Include the task text, the `.env` values that matter (never your real keys) and the relevant lines from `~/.hermie/outbound.jsonl`.

Bypasses of the privacy gate are treated as the highest severity. A false positive (clean text blocked) is a quality bug and can go in a normal issue with the eval case attached.
