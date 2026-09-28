# Roadmap

What is planned next and why, in the order it will be built. Each item lands as its own commit series with tests; the privacy invariants in [architecture.md](architecture.md) are not up for negotiation in any of them.

## Done in 0.2

- **Document attachments.** PDF, Word and Excel files dropped into the input box are converted to text on the machine and take the same caps and privacy path as text files.
- **Cloud provider choice.** `CLOUD_PROVIDER` selects DeepSeek, OpenAI, Anthropic or any OpenAI-compatible endpoint for the planner and the cloud-direct route; the outbound gate is the same for all of them.
- **Faster routing.** The three routing questions go to the judge as one structured request per sample; the contextual privacy question stays a request of its own because, measured on the eval set, it loses recall inside the combined form.

## Done after 0.2

- **Task graph.** The request flow runs as an explicit graph on `pydantic-graph` (route, snapshot, review loop, self-check, plan, cloud, fallbacks). Every task leaves a data-free trajectory line, and `hermie --graph` prints the diagram from the code.
- **Lesson memory.** Lessons from review-then-fix episodes and from problems the reviewer kept raising go into a local store with local embeddings; the most relevant ones are given to the executor before each step. The planner no longer receives lessons.
- **Routing calibration.** `hermie --calibrate` labels your recorded tasks from what happened after routing, sweeps the routing thresholds and proposes new values; `--apply` writes them to `.env` once there are enough labelled tasks. Privacy thresholds are never tuned from usage.

## Next: self-improvement, phase 2

A skill library: successful tool sequences turned into reusable, sandboxed skills the executor can call. Designed once phase 1 has produced enough trajectories to know which sequences recur.

## Also on the list

- A package-registry-only proxy so the executor can install dependencies without opening the sandbox to the network.
- Linux support through a bubblewrap implementation of the sandbox interface.
- Hermie as an MCP server, so other agents can delegate privacy-sensitive subtasks to it and get back only certified reports.
