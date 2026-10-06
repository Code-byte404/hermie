# Roadmap

What is planned next and why, in the order it will be built. Each item lands as its own commit series with tests; the privacy invariants in [architecture.md](architecture.md) are not up for negotiation in any of them.

## Done in 0.2

- **Document attachments.** PDF, Word and Excel files dropped into the input box are converted to text on the machine and take the same caps and privacy path as text files.
- **Cloud provider choice.** `CLOUD_PROVIDER` selects DeepSeek, OpenAI, Anthropic or any OpenAI-compatible endpoint for the planner and the cloud-direct route; the outbound gate is the same for all of them.
- **Faster routing.** The three routing questions go to the judge as one structured request per sample; the contextual privacy question stays a request of its own because, measured on the eval set, it loses recall inside the combined form.

## Done after 0.2

- **Task graph.** The request flow runs as an explicit graph on `pydantic-graph` (route, snapshot, review loop, self-check, plan, cloud, fallbacks). Every task leaves a data-free trajectory line, and `hermie --graph` prints the diagram from the code.
- **Lesson memory.** Lessons from review-then-fix episodes and from problems the reviewer kept raising go into a local store with local embeddings; the most relevant ones are given to the executor before each step. The planner no longer receives lessons.
- **Skill library.** Multi-step jobs that passed review become Markdown playbooks under `~/.hermie/skills/`, active once a second similar success (or the user) confirms them, given to the executor for similar steps, and retired when they stop helping.
- **Routing calibration.** `hermie --calibrate` labels your recorded tasks from what happened after routing, sweeps the routing thresholds and proposes new values; `--apply` writes them to `.env` once there are enough labelled tasks. Privacy thresholds are never tuned from usage.
- **Planner design phase.** In plan mode the planner first asks the questions that change the architecture or scope (multiple choice, recommended answer first), writes a detailed plan (goal, decisions, assumptions, architecture, steps with files and acceptance criteria, risks) to `PLAN.md`, and waits for your approval, change requests or rejection before anything runs. It can ask again or revise the plan during execution. Your answers leave the machine only through the same privacy gate as the task.
- **Data connectors, phase 1.** `asc` (App Store Connect and Apple Ads, read-only) lets Hermie answer business questions ("why did downloads drop last week?") by fetching the data itself. The data is processed by local models only: a business task is routed local, every cloud request is blocked, the sandbox goes offline, and the session stays locked until `/new` (a judge false positive locks it too). Apps are found by name, never by ID.
- **Data connectors, phase 2a.** GA4 through Google's analytics-mcp (run with `uvx`, pinned version, read-only) on the same connector framework and business lock. Opt-in: name it in `CONNECTORS` (`CONNECTORS=asc,ga4`), install `hermie[mcp]` and sign in with Google application default credentials. `/ga4` lists your properties and sets the session default. The server runs outside the sandbox with your Google Cloud credentials; its version and its dependencies' upload date are pinned.

- **Performance pass (2026-10).** Measured where a task's time went (the local executor and its judge calls, not the cloud planner) and fixed the traps: withdrawn tools answer with a note instead of "Unknown tool name" retries that failed the step, background judge checks use one sample (`BACKGROUND_JUDGE_SAMPLES`), one retry on an Ollama 500 from its tool-call parser, whole-file rewrites capped per file (`WRITE_REWRITE_LIMIT`), named output tools (`submit_report` / `submit_review`) with a plain-text fallback that points at them, a tolerant report schema and extra output retries for small local models.
- **cloud_exec route.** Opt-in (`CLOUD_EXEC=true`): for a task without private data that needs the workspace, the cloud model drives the local sandbox tools; every tool result is certified before it goes out, pattern hits become placeholders restored locally, an uncertifiable result or a cloud failure hands the step to the local executor with the tool history so far. Its own budgets (`CLOUD_MAX_TOOL_CALLS`, `CLOUD_MAX_REQUESTS`). Sensitive tasks keep plan mode, business tasks stay local.
- **GA4 with a service account.** Google blocks gcloud's own OAuth client for the Analytics scope on some accounts; `GA4_ADC_PATH` may point at a service-account key instead (its `project_id` is the quota project) once the account is a Viewer on the property.

## Next: data connectors, phase 2b and 2c

- Google Play (vitals, reviews, GCS exports) and Gmail, each as a plugin on the same connector framework and the same business lock.

## Also on the list

- Skills as callable, parameterized tools once the playbooks show which procedures are stable.
- A package-registry-only proxy so the executor can install dependencies without opening the sandbox to the network.
- Linux support through a bubblewrap implementation of the sandbox interface.
- Hermie as an MCP server, so other agents can delegate privacy-sensitive subtasks to it and get back only certified reports.
