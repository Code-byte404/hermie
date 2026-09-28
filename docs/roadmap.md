# Roadmap

What is planned next and why, in the order it will be built. Each item lands as its own commit series with tests; the privacy invariants in [architecture.md](architecture.md) are not up for negotiation in any of them.

## Done in 0.2

- **Document attachments.** PDF, Word and Excel files dropped into the input box are converted to text on the machine and take the same caps and privacy path as text files.
- **Cloud provider choice.** `CLOUD_PROVIDER` selects DeepSeek, OpenAI, Anthropic or any OpenAI-compatible endpoint for the planner and the cloud-direct route; the outbound gate is the same for all of them.
- **Faster routing.** The three routing questions go to the judge as one structured request per sample; the contextual privacy question stays a request of its own because, measured on the eval set, it loses recall inside the combined form.

## Next: the task graph

The request flow in `core.py` (route, four handlers, review loop, self-check, escalation, cloud fallbacks) becomes an explicit graph on `pydantic-graph`, which Pydantic AI already depends on. Nothing a task does changes. What is gained:

- Named nodes with a recorded trajectory per task (`trajectories.jsonl`: node timings, statuses and structured signals, never task text or tool output).
- A Mermaid diagram generated from the code (`hermie graph`) instead of a hand-drawn one.
- Stable places to attach the self-improvement pieces below.

Resuming an interrupted task from a checkpoint is not part of this step; the task state holds live sandbox and agent handles.

## Then: self-improvement, phase 1

Two loops that read Hermie's own local record and feed the next task. Both stay entirely on the machine.

1. **Lesson memory.** Lessons written after a review-then-fix episode (and after a problem the reviewer raised twice without a fix) go into a local store with tags and local embeddings. Before execution, the few lessons most similar to the task are injected instead of the whole `## Lessons` block of `AGENT.md`; lessons that keep being injected without a first-round review pass are ranked down. The planner stops receiving the lessons section at all.
2. **Routing calibration.** `hermie calibrate` labels each recorded task's route from what followed (local work that had to escalate, planner runs a single local step would have finished, forced routes), sweeps the routing thresholds on the recorded signals plus the curated eval cases, and prints the proposal. `.env` changes only with `--apply` and only above a minimum number of labelled tasks. Privacy thresholds are excluded from tuning on purpose.

## Later: self-improvement, phase 2

A skill library: successful tool sequences turned into reusable, sandboxed skills the executor can call. Designed once phase 1 has produced enough trajectories to know which sequences recur.

## Also on the list

- A package-registry-only proxy so the executor can install dependencies without opening the sandbox to the network.
- Linux support through a bubblewrap implementation of the sandbox interface.
- Hermie as an MCP server, so other agents can delegate privacy-sensitive subtasks to it and get back only certified reports.
