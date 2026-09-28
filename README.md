<p align="center">
  <img src="assets/logo.png" width="160" alt="Hermie, a hermit crab peeking out of its shell">
</p>

<h1 align="center">Hermie</h1>

<p align="center">A local-first coding and document agent for macOS.<br>
Local models do the work inside a sandbox. The cloud planner only ever sees certified, privacy-free text.</p>

<p align="center">
  <a href="LICENSE"><img alt="MIT" src="https://img.shields.io/badge/license-MIT-orange"></a>
  <img alt="macOS Apple Silicon" src="https://img.shields.io/badge/platform-macOS%20Apple%20Silicon-lightgrey">
  <img alt="Python 3.12" src="https://img.shields.io/badge/python-3.12-blue">
</p>

![A plan-mode task whose text contains a customer's phone number. The local panes show the real number and the report the executor wrote. The Outbound tab on the right shows everything the cloud planner received: the task with the number replaced by a placeholder, the plan acknowledgement and two structured step reports. No file contents, no diff, no answer.](assets/screenshot.png)

*The task above contains a customer's phone number. The cloud planner (right pane) got `<CN_MOBILE_1>` instead; the number, the CSV and the finished report never left the machine.*

## What it does

Like a hermit crab, Hermie carries its own shell and only pokes its eyes out.

- **Repetitive and simple work stays local.** An Ollama model runs the task with file and shell tools inside a macOS Seatbelt sandbox: write access only to the workspace, no network, no keychain, no `~/.ssh`.
- **Hard work gets a cloud planner, not a cloud executor.** For tasks that need overall planning, a cloud model (DeepSeek by default; OpenAI, Anthropic or any OpenAI-compatible endpoint) writes the plan and delegates steps one by one. It receives the de-identified task and fixed-structure step reports. It never receives file contents, diffs, or the executor's answer.
- **Nothing leaves without a certificate.** Every outbound message passes a three-layer privacy gate (regex recognizers for phones, IDs, cards, secrets; Presidio NER; a local judge model for context). The cloud client only accepts the `CleanText` type that the gate produces. If any layer errors, the text counts as sensitive.
- **The executor has to prove it.** A local reviewer reads the actual workspace diff before a task is allowed to report done. Failed reviews go back to the executor for a bounded number of fixes.
- **You can see everything.** The Outbound tab shows every message sent to the cloud, verbatim. The Changes tab shows the diff. Every task is snapshotted first and can be rolled back.

## How a task is routed

| Route | Who runs it | When |
|---|---|---|
| `local` | local executor + local review | repetitive, simple, or touches private data |
| `local_verify` | local executor, then a judge self-check | medium difficulty; escalates to plan or cloud if the check fails |
| `cloud` | cloud model alone | no workspace needed, no private data (explanations, tutorials) |
| `plan` | cloud planner driving the local executor step by step | needs planning across many steps |

Routing is a pure function of three parallel signals: the privacy check, three yes/no questions to a local judge model, and a RouteLLM complexity score. Any hint that the task needs the workspace keeps it local. Any cloud error falls back to local.

## Requirements

- macOS on Apple Silicon. The sandbox is Seatbelt, transcription is mlx-whisper, speech is `say`.
- [Ollama](https://ollama.com) running locally with an executor model and a judge model pulled. Defaults: `qwen3.8:27b-mlx` as executor and reviewer, `gemma4:12b` as judge. A 32 GB machine runs both; a smaller judge model is the biggest speed win.
- A cloud API key (DeepSeek by default; `CLOUD_PROVIDER` switches to OpenAI, Anthropic or an OpenAI-compatible endpoint), only if you want the `cloud` and `plan` routes. Everything else works fully offline.

## Install

```bash
git clone https://github.com/Code-byte404/hermie.git && cd hermie
conda env create -f environment.yml && conda activate hermie
python -m spacy download zh_core_web_sm
cp .env.example .env          # add CLOUD_API_KEY if you want cloud routes; pick your Ollama models
```

Or with plain pip into any Python 3.12 environment:

```bash
pip install -e ".[router,voice]"   # drop the extras you do not need
python -m spacy download zh_core_web_sm
```

## Run

The directory you start in is the workspace. The sandbox only lets the executor write there. `$HOME` and `/` are refused.

```bash
cd ~/projects/my-app
hermie                     # full-screen UI; high-risk commands prompt for approval
hermie --auto              # no approval prompts; sandbox, gate, snapshots and audit unchanged
hermie --json "task" file  # headless: JSON event stream, then the final result (file or directory as material)
```

`start.sh` in the repo does the whole warm-up: activates the env, starts Ollama if needed, pulls missing models, opens the UI.

Inside the UI: type a task and press `Enter`. Drag a file or folder from Finder into the input box to attach it: its path stays in the text, a 📎 line shows what will be attached, and the content (text extracted from PDF / Word / Excel, a file tree for folders) is read locally and goes through the privacy gate with the task, so nothing unredacted leaves the machine. `/` opens the command list. `F2` switches default/auto mode, `F5` records a voice task, `F6` toggles speech. The full list is in the [user guide](docs/guide.md).

## Where the privacy guarantee comes from

1. **Types.** `CleanText` can only be constructed by `PrivacyGate.certify()`. The cloud agents accept nothing else.
2. **An outbound guard on every cloud request.** Any message part that is not already certified is re-checked on the spot or the request is aborted.
3. **A kernel boundary, not a prompt.** The executor's reads, writes and commands run under `sandbox-exec` with a fixed environment allowlist. The API key never enters the sandbox.
4. **Fail closed.** A detector exception means "sensitive". A cloud exception means "run it locally".
5. **Hashes in the audit log.** `~/.hermie/audit.jsonl` records the route and the input's SHA-256, never the input. `~/.hermie/outbound.jsonl` records exactly what was sent.

What the gate cannot promise (probabilistic name detection, uncalibrated thresholds, the no-sandbox flag) is written down in [SECURITY.md](SECURITY.md).

## Documentation

- [User guide](docs/guide.md): the UI, `AGENT.md`, the self-verification loop, voice, web access, examples, evals and known limitations.
- [Roadmap](docs/roadmap.md): the task graph, the self-improvement loop and what comes after.
- [Architecture](docs/architecture.md): request flow, module responsibilities, privacy invariants.
- [Design document](docs/design.md): the original design and its reasoning.
- [Contributing](CONTRIBUTING.md) and [Security](SECURITY.md).

## Status

Alpha. The routing thresholds shipped in `.env.example` are starting points; `evals/` has the tooling to calibrate them on your own requests. The test suite (fake models, real sandbox and detectors) runs with `pytest -q` and needs no Ollama or cloud key.

## License

MIT. See [LICENSE](LICENSE).
