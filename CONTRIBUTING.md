# Contributing to Hermie

Thanks for looking under the shell. This page covers the environment, the tests, and the few rules that keep the privacy guarantees intact.

## Setup

Hermie runs on macOS on Apple Silicon (the sandbox is Seatbelt, transcription is mlx-whisper, speech is `say`).

```bash
conda env create -f environment.yml
conda activate hermie
python -m spacy download zh_core_web_sm
cp .env.example .env            # models and thresholds; a DeepSeek key is only needed for cloud routes
```

Everything below assumes the env is active.

## Tests

```bash
pytest -q                                      # full suite, about two minutes, no Ollama or DeepSeek needed
pytest -q tests/test_pipeline.py -k plan_mode  # one area
pytest -q tests/test_tui.py                    # Textual Pilot UI tests
```

The models are faked (`tests/conftest.py`: `FakeJudge` drives routing, `Script` plays back executor/planner turns and records what the model saw). Presidio, the Seatbelt sandbox and snapshots are real. When you add a feature that sends anything to the cloud, add a test that asserts on `Script.sent_text()` that the private data is absent.

`evals/` holds labelled cases for calibrating thresholds against real models. The rules-layer privacy cases double as a regression test (`tests/test_evals.py`): a new recognizer or a changed pattern must keep them passing.

## Rules that are not negotiable

These are the privacy invariants. A pull request that weakens one will not be merged, however good the feature is. The reasoning behind each is in [docs/architecture.md](docs/architecture.md).

- `CleanText` is constructed only in `privacy.py` via `certify()` or `trusted_template()`. Never pass user-derived text through `trusted_template()`.
- Any exception in Presidio or the judge means "sensitive". Any DeepSeek failure falls back to local.
- The planner gets only redacted or abstracted task text, the workspace overview, the project doc and `format_report()` output. Never `answer`, never file contents, never the diff.
- History compression and abstraction use `models.compressor()`, a local model. `ModelFactory.reviewer()` stays local because it sees diffs.
- The env passed into the sandbox is a fixed allowlist. Never pass `os.environ` through.
- No token accounting outside `ActivityTracker` and `OllamaJudge.usage_sink`, or the stats double-count.

## Conventions

- Python 3.12, type hints, `asyncio`. Routing logic lives in the pure `policy.decide()` and is tested directly.
- Comments, prompts, log messages, docs and UI strings are in English. No Chinese characters anywhere in the repo; the privacy recognizers target Chinese-format PII, but that is logic, not text.
- Slash commands are declared once in `hermie/tui/commands.py`; the popup and `/help` are generated from that table.
- `sounddevice` and `mlx_whisper` must stay lazy imports (tests assert they are never imported by the core).
- Pin dependency versions in `environment.yml` and `pyproject.toml` together.

## Pull requests

1. Open an issue first for anything that changes routing, the gate or the sandbox, so the design can be discussed before the code.
2. Keep one concern per PR. Run `pytest -q` before pushing.
3. If you changed a recognizer or a prompt, run `python evals/run_evals.py privacy` (rules only, seconds) and paste the summary in the PR.
4. Describe what the cloud model can now see that it could not see before, even if the answer is "nothing".
