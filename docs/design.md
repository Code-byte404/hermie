# Local-First Hybrid Agent Framework - Design Document

Sep 27, 2026 - @Frank

## 1. Overview

This framework is a local-first agent that runs on macOS: simple, repetitive work is done entirely by local Ollama models; complex tasks or tasks that need overall planning go to DeepSeek, but DeepSeek only ever sees certified, privacy-free content. The executor can operate on documents and code locally and autonomously, and a fully automatic mode that never asks for human confirmation is supported.

**Goals**

- Private data never leaves the machine: anything sent to the cloud must pass the privacy gate, and this is guaranteed twice over, by the type system and by the kernel sandbox.
- Balance cost and capability: whatever can be done locally stays local; DeepSeek is called only when the local model genuinely cannot do the job well.
- Autonomous operation: the executor can read and write files and run commands inside the sandbox, and in skip-permissions mode it never interrupts the user.
- Transparent process: the user can always see who executed each step and what left the machine.

**Non-goals**

- No defense against malicious programs that actively try to escape the sandbox; the threats defended against are local-model mistakes and prompt injection.
- No use of native macOS applications (Word, Pages, Numbers, etc.) for document processing.
- No multi-user support, remote access or web UI in the first version.

**Typical scenarios**: batch-organizing and converting local documents; modifying, running and testing code in a local repository; analyzing material that contains customer or internal information and using DeepSeek to draft a plan, while the raw material stays local throughout.

## 2. Design Principles

The following five principles take precedence over any feature requirement; everything else in the design follows from them.

1. **Separate the control plane from the data plane.** The control plane is "what to do, how far we got, what went wrong" and may flow between cloud and local. The data plane is the raw material, intermediate files and final results, and it always stays local. The final deliverable handed to the user is read from the local workspace, not taken from the planner's reply.
2. **Fail closed.** If privacy detection, the judge model or any gatekeeping step errors out, the content is treated as "contains private data" and stays local. Better to over-use the local model than to risk sending data out.
3. **Outbound is guaranteed by types.** Content sent to DeepSeek must be of type `CleanText`, and `CleanText` can only be produced by the privacy gate after a full check has passed. There is no such thing as "some code path forgot to check".
4. **Boundaries are enforced by the kernel.** Everything the executor does happens inside a Seatbelt sandbox; file and network boundaries are enforced by the operating system, not by path checks in code.
5. **What gets skipped is approval, not boundaries.** skip-permissions only turns off human confirmation; the sandbox, privacy gate, snapshots and audit log stay active in every mode.

## 3. Overall Architecture

The system has four layers: UI, entry router, agents and execution environment. Only the planner lives in the cloud, and the privacy gate is its sole channel to the local machine.

&#91;embedded content: System architecture - the boundary between local machine and cloud\]

The executor reads and writes the local workspace inside the sandbox, and the final result is delivered to the UI directly from the workspace; the planner receives only de-identified content and structured reports, and issues delegated steps to the executor.

The entry router chooses one of three processing modes for each request:

| Mode | Trigger | Participants | Outbound? |
| --- | --- | --- | --- |
| Local only | Simple, repetitive work; or contains private data and needs no planning | Executor | No |
| Local + self-check | No private data, but conflicting signals or low confidence | Executor; escalates to DeepSeek if the self-check fails | Only on escalation |
| Plan mode | Needs overall planning, or the task is hard | Planner + executor | Only de-identified content and reports |

## 4. Entry Router Layer

When a request arrives, four checks run locally in parallel, and then a pure-function policy decides the processing mode. All detection components run on the local machine because they see the raw material.

| Component | Question answered | Output |
| --- | --- | --- |
| Presidio | Does it contain rule-detectable sensitive entities (ID card, mobile number, bank card, email, IP, custom keywords, person names)? | Entity list with positions |
| Judge - privacy | Does it contain contextually sensitive information (unannounced internal plans, health or financial status, etc.)? | Probability 0-1 |
| Judge - task type | One of: repetitive, simple, complex, planning | Choice + confidence |
| Judge - difficulty | One of three levels: easy, medium, hard | Level + confidence |
| RouteLLM | Probability that the strong model clearly beats the weak one | Probability 0-1 |

**Routing rules** (matched in order):

1. Task type is "repetitive" with sufficient confidence: fixed local, other signals ignored.
2. Task type is "planning" with sufficient confidence, or difficulty is "hard", or "complex" with a RouteLLM win rate above the threshold: lean cloud.
3. Difficulty is "easy" and RouteLLM does not object, or "easy" with a low RouteLLM win rate: lean local.
4. None of the above: mark as "uncertain".

Privacy is then layered on top: if private data is present, "lean cloud" becomes plan mode and everything else becomes local only; with no private data, "lean cloud" goes to plan mode or is completed directly by DeepSeek, and "uncertain" goes to local + self-check.

**Constraints on using RouteLLM**: only its BERT router is used, and the weights are loaded directly with transformers without importing the routellm package. The reason is that its mf and sw\_ranking routers need to call the OpenAI API to generate embeddings, and its routing module creates an OpenAI client at import time. The weights were trained on mostly-English Chatbot Arena data, so their discriminative power on Chinese must be measured; if it turns out poor, RouteLLM can be disabled and the judge model relied on entirely.

**Threshold calibration**: before going live, collect 200-500 real privacy-free requests, have both Ollama and DeepSeek complete them, label "was local good enough", and tune the routing thresholds, confidence floors and self-check threshold accordingly. The target is a sufficiently low rate of "should have gone to the cloud but stayed local" while keeping the cloud share within budget.

## 5. Judge Model (Jev-like)

The judge model is a local gatekeeping service used throughout the system; it only makes structured judgments and never generates prose. It must run on the local machine because it sees all the raw material; the cloud-hosted Jev (TypeSafe API) therefore cannot fill this role.

**Interface**: three primitives, corresponding to Jev's Choice, Score and Noul. The rest of the system depends only on these three methods, so the implementation can be swapped without touching callers.

| Primitive | Input | Output |
| --- | --- | --- |
| choice | material, question, set of options | chosen option, per-option probabilities, confidence |
| score | material, question, ordered levels | level, per-level probabilities, confidence |
| noul | material, yes/no statement | probability of "yes" |

**Default implementation**: a small Ollama model (e.g. qwen3:4b) with JSON Schema-constrained output; each question is sampled 3 times and the vote ratio approximates probability and confidence. It works but is only moderately calibrated; when a dedicated local Jev-like model is available, swap it in directly.

**Responsibilities**:

- Entry routing: task type, difficulty, contextual privacy.
- Command guard: risk-score the command the executor is about to run (in default mode this decides whether to prompt for approval).
- Taint tracking: judge whether a tool output contains contextually sensitive information.
- Progress supervision: judge whether the executor is stuck or whether the planner should re-plan.
- Result self-check: judge whether the local result fully completes the task, deciding whether to escalate to DeepSeek.

**Usage conventions**: questions must be atomic, one thing at a time; complex judgments are split into several small questions and combined by weight in code. Any instruction appearing in the material is treated only as data to be judged and must never be executed.

## 6. Privacy Gate and Outbound Control

The privacy gate is the only channel between the local machine and DeepSeek; all outbound content must be certified by it.

**Detection** has two layers: Presidio handles rule-detectable entities, using the Chinese spaCy model plus custom recognizers (ID card numbers with checksum validation, mobile numbers, bank card numbers with Luhn check, email, IPv4, custom keywords); the judge model handles contextually sensitive information the rules cannot catch. If either layer says sensitive, it is sensitive.

**De-identification** comes in two forms, and the result must pass the gate again before going out:

- Placeholder substitution: when only rule-detectable entities are present, entities are replaced with placeholders such as `<CN_MOBILE_1>`, and the mapping table is stored locally only.
- Abstract rewriting: when contextually sensitive information is present, a local model rewrites the task into an abstract description with no specific names, organizations, numbers or internal details.

If both fail, the task is handled locally end to end.

**The CleanText type constraint**: the DeepSeek client accepts only `CleanText`, and `CleanText` can only be created by the gate's certify method, which re-runs the full check. Passing a plain string raises immediately. Prompt templates hard-coded in the source are created through a separate "trusted template" method, restricted to constants that contain no user data.

**Taint tracking**: after every executor tool call, the output is privacy-checked. Once sensitive content has been read, that executor session is marked "tainted". Nothing from a tainted session may enter the planner's context directly; it can only be passed as a structured report that went through the gate.

**Second check before sending**: even if the routing stage found no private data, the content is re-certified right before it is actually sent.

**Other exits**: the following channels could also leak data and need to be closed separately:

- Context compression: long-session summaries must use a local model.
- Observability reporting: cloud observability services such as Logfire are not enabled.
- The executor itself: the sandbox has network access (package installs, clones), so its commands are an exit. Network commands (curl / wget / ssh / scp / rsync / pip install) are rated high risk and need approval in default mode; auto mode trusts them (see section 9).

## 7. Agent Layer (Pydantic AI)

The agent layer uses a split "planner + executor" structure; the two agents each own a separate session and share no message history. This avoids the leak where "switching models within one session hands the full history to the new model".

**Planner**

- Model: DeepSeek (deepseek-v4-pro for planning, deepseek-v4-flash for simple cloud tasks).
- Tools: only "delegate to executor"; no file or command permissions of any kind.
- Context: the de-identified task description plus the clean reports returned by the executor.
- Responsibilities: break down the task, make and adjust the plan, decide when the whole thing is done.

**Executor**

- Model: a local Ollama model (recommended 8B or larger with good tool-calling ability, e.g. qwen3:8b).
- Tools: run commands, read files, write files and edit files inside the sandbox. All tools execute through the sandbox subprocess. Two kinds of tool run in the controller process instead, each with a narrow, logged interface: web_fetch / web_search (section 6) and screenshot (section 9), which hands the executor an image of the iOS simulator or the Mac screen.
- Output: a fixed-structure report (see section 8), never free text.
- Memory: local history is kept across delegations and compressed by a local model when it grows too long.

**Delegation mechanism**: when the planner calls the delegate tool, the tool internally starts one executor run; when the executor finishes, it returns a report, which after validation and the gate is returned to the planner as the tool result. In local-only mode the planner is never started and the executor runs directly.

**Mapping gate points to Pydantic AI mechanisms**: the gatekeeping logic is packaged as three reusable capabilities (outbound guard, command guard, taint tracker) that are attached to the different agents as needed.

| Need | Pydantic AI mechanism | Attached to |
| --- | --- | --- |
| Final check before sending to DeepSeek | Pre-model-request hook scanning all messages | Planner |
| Command risk judgment and approval | Pre-tool / wrap-tool-execution hook | Executor |
| Mark the session when sensitive content is read | Post-tool hook + run-context state | Executor |
| Limit the tools visible at each step | prepare\_tools | Both |
| Reports contain no private data | Structured output + output validator | Executor |
| Prevent infinite loops and runaway cost | Usage limits (request count, token count) | Both |
| UI events | Hooks at every level emit events | Both |

**Versioning**: the capability and hook APIs were introduced in v1.71 (March 2026) and are still changing, so the version must be pinned.

## 8. Executor Report Format

The report is the only thing on the control plane that flows from local to cloud, so it uses a fixed structure rather than free text: structured data is easier to check, and it limits the executor's room to "casually" carry raw data along.

| Field | Type | Description |
| --- | --- | --- |
| status | Enum: done / failed / needs clarification / partial | Result of this delegation |
| steps\_done | List of strings | Which steps were completed, actions only |
| artifacts | List: path + type + order of magnitude | Output files, path and summary only (e.g. "CSV, 14 rows"), no content |
| issues | List of strings | Problems encountered and error types |
| question | Optional string | The question, when the planner needs to clarify |

**Validation flow**:

1. Pydantic validates structure and types; on a format error the framework automatically asks the executor to rewrite.
2. The output validator runs Presidio and the judge model on every text field; when private data is found it raises "retry" and tells the executor to drop the specifics, e.g. change "customer Zhang Wei's order has been processed" to "1 customer order has been processed".
3. If the report still fails after the retry limit, only the status field is returned to the planner, the other fields are discarded, and the UI shows a notice.
4. Once it passes, the gate certifies it as CleanText and it is returned to the planner as the tool result.

**File paths themselves may contain private data** (e.g. a file named after a customer). Paths go through the same check and are replaced by a number when necessary.

## 9. Sandbox and Execution Environment (macOS)

Everything the executor does runs inside the macOS Seatbelt (sandbox-exec) sandbox. Seatbelt is enforced by the kernel, is inherited by all child processes once applied, and cannot be lifted from inside. Apple has marked it deprecated, but it still works and still receives security fixes on Sequoia and Tahoe, and there is currently no third-party alternative.

**Process split**:

- Controller process (outside the sandbox): entry routing, planner, privacy gate, judge model, agent loop. It can reach DeepSeek and the local Ollama, but never performs actions requested by a model.
- Executor process (inside the sandbox): all of the executor's tools, including reading and writing files, run in the sandbox subprocess, never directly in the controller. This way the file boundary has exactly one enforcement point, the kernel, and does not depend on path checks in code.

**Sandbox rules**:

| Resource | Rule |
| --- | --- |
| File writes | Workspace and temp directories only |
| File reads | System directories, workspace, toolchains; \~/.ssh, cloud credentials, browser data etc. denied |
| Network | Open (installing dependencies was otherwise impossible); network commands need approval in default mode, package-manager caches live in the sandbox temp dir |
| Keychain and credentials | Denied; credential channels such as the SSH agent are removed from the environment |
| Apple Events and launching other apps | Denied (see below) |
| Screen capture from the shell | Denied (screencapture, window server); screenshots go through the screenshot tool (see below) |
| Resource limits | Per-command timeout; maximum steps per task |

**No native app calls**: having Word, Pages, Numbers etc. process files via AppleScript, or opening files with the open command, amounts to asking a fully privileged app outside the sandbox to act on the executor's behalf, which bypasses the sandbox. Document processing uses command-line tools and libraries instead: Python's docx, Excel and PDF libraries, pandoc, and LibreOffice in headless mode.

**Mac toolchain**: the Xcode command-line tools work inside the sandbox as they are: xcodebuild (derived data inside the workspace), swift build / test, xcrun simctl (list, boot, install, launch, openurl, appearance; the simulator itself is a separate process outside the sandbox, so it keeps its network) and AXe for simulator UI automation (describe-ui, tap, type, swipe, button). The executor prompt carries a cheat sheet for them when xcodebuild is installed, and the recon line tells the planner about booted simulators and AXe. The one thing the sandbox cannot do is capture an image: the `screenshot` tool runs in the controller process with a fixed argv (`xcrun simctl io <device> screenshot` or `screencapture -x`), like the web tools, saves the full PNG under `data_dir/screenshots` (never the workspace, so it stays out of diffs and snapshots) and returns a downscaled copy to the local vision model as an image. Simulator screenshots show the project's own app on a clean device and carry no taint. A Mac-screen screenshot may show anything on the display and cannot be scanned by the gate, so it marks the task as tainted (fail closed) and in default mode asks for approval once per session; `SCREENSHOT_MAC=false` removes that target. The image never goes further than the local model: reports and the planner cannot carry it.

**Workspace location**: keep it outside Desktop, Documents, Downloads and other directories protected by macOS privacy permissions (TCC), to avoid permission prompts interrupting automatic runs and to avoid granting the terminal "Full Disk Access".

**Snapshots and rollback**: the sandbox stops the executor from crossing boundaries, but not from breaking files inside them. A snapshot is taken automatically before every executor task: code projects use a git commit checkpoint; document directories use an APFS copy-on-write clone (near-instant, no extra space). The UI offers one-click rollback to the pre-task state.

**Threat model**: Seatbelt defends against boundary violations caused by local-model mistakes and prompt injection, not against malicious programs actively trying to escape. For stronger isolation, use a separate macOS virtual machine or a separate user account; Docker runs Linux on the Mac and cannot use macOS tools, so it is not used.

## 10. Run Modes and Permissions

The system offers three run modes. skip-permissions corresponds to "auto mode": it only turns off human approval; the sandbox, privacy gate, snapshots and audit log are unchanged.

| Item | Default mode | Auto mode (skip-permissions) | No-sandbox mode |
| --- | --- | --- | --- |
| Human approval | Prompt for high-risk operations | All skipped | All skipped |
| Seatbelt sandbox | On | On | Off |
| Executor network | Disabled | Disabled | Unrestricted |
| Privacy gate | On | On | On |
| Automatic snapshots | On | On | On |
| Audit log | On | On | On |
| How to enable | Default | Command-line flag or UI toggle | Separate dangerous flag + startup warning |
| UI color | Normal | Yellow | Red |

**Approval rules in default mode**: the judge model risk-scores every command (low / medium / high). Read-type operations and ordinary writes inside the workspace go through directly; high-risk operations such as deletion, bulk overwrites and installing software prompt a dialog showing the command, the risk score and the options (allow, deny, allow for this session).

**No-sandbox mode** is not bundled with skip-permissions and must be enabled separately and explicitly, so that someone who only wants to skip confirmations does not tear down the boundary by accident. The privacy gate cannot be disabled in any mode, because it is the reason the framework exists.

## 11. Frontend (Textual full-screen split layout)

The frontend is a Textual full-screen split-pane UI rendered by Rich underneath. The design focus is making the data flow obvious at a glance: the user always knows who executed each step and what left the machine.

```
+ Auto mode | outbound 2 - all passed gate | executor qwen3:8b | planner deepseek-v4-pro | ~/work +
+- Plan ---------------+- Chat -----------------------+- [Execution log] Outbound Changes Audit -+
| Route: plan mode     | > user instruction           | $ python summarize.py                     |
| v 1 Aggregate data   | (cloud) planner reply (stream)|   v by_region.csv (14 rows)               |
| * 2 Compute QoQ      | (lock) executor report        |                                           |
+----------------------+------------------------------+-------------------------------------------+
| > type an instruction                                        Esc interrupt - F2 switch mode      |
+---------------------------------------------------------------------------------------------------+
```

| Area | Content |
| --- | --- |
| Top bar | Run mode (color-coded), outbound count this session, current models, workspace path |
| Left pane | Routing decision and reasons, plan checklist (ticked live as reports arrive) |
| Middle pane | Chat; planner replies shown as streaming Markdown; local and cloud distinguished by icon |
| Right pane tabs | Execution log (commands and output), outbound record (the exact text sent to DeepSeek each time), file changes (diff view), audit log |
| Bottom | Multi-line input box, shortcut hints |

**Core decoupled from UI**: the core never calls the UI directly; instead it emits events from Pydantic AI hooks at every level (route decided, plan updated, command started, command output, outbound sent, approval request, report arrived) and the UI subscribes and renders. The approval request is the only event that needs a reply: the core waits for the user's choice, and in auto mode it passes straight through. The same core also supports a headless mode that prints JSON, for scripting.

**Key technical requirements**:

- Never block the UI: Presidio, spaCy, RouteLLM inference and waiting on sandbox subprocesses all run in Textual background worker threads.
- Output throttling: the execution log caps its line count and flushes in batches; streaming Markdown uses Textual v4's built-in background updater.
- Interruption: Esc aborts the current step, kills the subprocess inside the sandbox, and lets the user add instructions and continue.
- Narrow screens: the left and right panes can be collapsed with shortcuts.
- Copying: use Textual's built-in text selection, and provide a command to export the session to a file.

**Slash commands**: switch mode, force local or cloud, view outbound record, roll back to a snapshot, view audit log, view cost and token usage, export session.

**Security note**: textual-serve can turn the UI into a web page, but this app can run commands and see private data; if used, bind only to the loopback address.

## 12. Observability, Audit and Logs

All observability data is stored locally only; the audit log contains no raw text, and a full session transcript is only exported explicitly by the user.

| Record | Content | Contains raw text? | Location |
| --- | --- | --- | --- |
| Audit log | Time, input hash, route mode and reasons, all signals, execution backend, outbound count, latency | No | Local JSONL |
| Outbound record | Full content of every message sent to DeepSeek | Only certified clean content | Local, viewable in the UI |
| Command record | Every command the executor ran, exit code, duration | Command text may contain paths | Local |
| Session export | Full conversation and output | Yes | User-chosen location |
| Trace data | Model call chains, timing | Depends on config | Local OpenTelemetry backend only, Logfire not enabled |

The outbound record lets the user verify the gate's effect with their own eyes, spot misses or over-blocking, and tune thresholds accordingly. The terminal scrollback also contains executor output; this is stated in the in-app help.

## 13. Tech Stack and Dependencies

All Python, running on macOS. Versions are pinned at implementation time.

| Layer | Component | Purpose |
| --- | --- | --- |
| Agent framework | Pydantic AI | Planner and executor, capabilities and hooks, structured output |
| Local models | Ollama (executor qwen3:8b, judge qwen3:4b) | Execution, judgment, abstract rewriting, context compression |
| Cloud model | DeepSeek API (deepseek-v4-pro / deepseek-v4-flash) | Planning and complex tasks |
| Privacy detection | Presidio + spaCy Chinese model (zh\_core\_web\_sm, swappable for trf) | Entity recognition |
| Complexity routing | RouteLLM BERT weights + transformers + torch | Strong-model win rate |
| Sandbox | macOS Seatbelt (sandbox-exec) | Executor isolation |
| Snapshots | git, APFS clones | Rollback |
| Document processing | python-docx, openpyxl, PDF libraries, pandoc, LibreOffice headless | Processing documents inside the sandbox |
| Frontend | Textual + Rich | Full-screen split layout |
| Testing | pytest, Textual Pilot, snapshot-test plugin | Logic and UI tests |
| Observability | OpenTelemetry (local backend) | Optional tracing |

**Reusable parts of the existing prototype**: the previously implemented privacy gate (Chinese recognizers, CleanText, de-identification and restoration), the judge interface and its Ollama implementation, local RouteLLM loading, the routing policy function and the fake-model tests can all be migrated directly into the new architecture.

## 14. Risks and Known Limitations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Small spaCy model misses Chinese person names | Names may slip past the rules layer | Judge model contextual check as backstop; switch to the trf model; add customer lists to the keywords |
| Judge model only moderately calibrated (vote approximation) | Routing and privacy judgments not precise enough | Conservative thresholds; replace when a dedicated local Jev-like model exists |
| RouteLLM's discriminative power on Chinese unknown | Complexity signal may be useless | Decide whether to disable after measuring |
| Small local models call tools unreliably and get lost in long tasks | Execution failures or infinite loops | Choose 8B+ models; few, simple tools; usage limits; judge supervises progress |
| Executor report carries private data | Data flows to the cloud | Triple check: structured format + output validator + gate |
| Prompt injection (malicious instructions inside files) | Executor misled | Tool output is always treated as data; network commands need approval in default mode (the sandbox itself is online) |
| Seatbelt is deprecated | Future macOS may remove it | Sandbox layer is a replaceable interface; VM as fallback plan |
| Textual's company has shut down; the Pydantic AI capability API is new | Interface changes or slower maintenance | Pin versions; core decoupled from UI |
| Chinese input method misbehaves in the full-screen UI | Poor input experience | Verify in the everyday terminal before writing the UI |
| Executor corrupts files inside the workspace | Data damage | Automatic snapshots and one-click rollback |

## 15. Implementation Roadmap

Work proceeds in five phases, "local first, then cloud, then UI", and each phase must pass its gate test before the next begins.

&#91;embedded content: Implementation roadmap - 5 phases, 4 gates\]

The executor comes first because it carries the most risk and uncertainty (sandbox rules, local-model capability), and because local-only mode becomes usable at that stage already. Every phase first tests the privacy guarantees with fake models before connecting real ones.

## 16. Open Questions

- [x] Does the executor need network access to install dependencies when working on code? Yes: the sandbox is online (2026-09-29). A local proxy that logs hosts and blocks non-registry hosts after taint is the stricter option if the exit needs closing again.
- [ ] Is a usable local Jev-like judge model already available? The specific model determines the first replacement implementation of the judge interface.
- [ ] Which terminal is used day to day (iTerm2, Ghostty, WezTerm or the system Terminal)? This determines the target for the phase-0 Chinese input verification.
- [ ] What executor and judge model sizes can the local hardware (memory, chip) run at the same time?
- [ ] In plan mode, when an executor report still fails after repeated retries, return status only, or pause and ask the user to step in?
- [ ] Is Linux support needed? If so, add a bubblewrap implementation to the sandbox layer.

## 17. Reference Links

| Project | Repository | Clone URL | Notes |
| --- | --- | --- | --- |
| RouteLLM | [github.com/lm-sys/RouteLLM](https://github.com/lm-sys/RouteLLM) | `https://github.com/lm-sys/RouteLLM.git` | `lm-sys/routellm` points to the same repository (GitHub URLs are case-insensitive) |
| Presidio | [github.com/data-privacy-stack/presidio](https://github.com/data-privacy-stack/presidio) | `https://github.com/data-privacy-stack/presidio.git` | Moved here from the original microsoft/presidio; docs at [data-privacy-stack.github.io/presidio](https://data-privacy-stack.github.io/presidio) |
| Pydantic AI | [github.com/pydantic/pydantic-ai](https://github.com/pydantic/pydantic-ai) | `https://github.com/pydantic/pydantic-ai.git` |  |
