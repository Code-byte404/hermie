# Contributing to Hermie

Thanks for looking under the shell. This page covers the setup, the tests, and the rules that keep the privacy guarantees intact.

## Setup

Python 3.12 on macOS or Linux.

```bash
conda env create -f environment.yml      # or: python3.12 -m venv .venv && pip install -e ".[test]"
conda activate hermie
python -m spacy download en_core_web_lg
python -m spacy download zh_core_web_sm  # optional: the Chinese-engine tests skip without it
```

## Tests

```bash
pytest -q                                   # the default suite; no network, no Ollama, no API key
pytest -q tests/test_server.py              # one module
pytest -m live tests/test_live.py::test_claude_code -s   # a real client through a real proxy (needs the client installed and logged in)
```

The default suite excludes live tests (`addopts = "-m 'not live'"`). Upstreams are faked with `httpx.MockTransport` (the `Upstream` recorder in `tests/test_server.py` keeps every request that reached the "cloud", so tests assert that a value is absent there). The judge is faked (`FakeJudge` in `tests/test_gate.py`). Presidio and spaCy are real.

`evals/` holds labelled privacy cases. The rules-layer cases double as a regression test (`tests/test_evals.py`): a new recognizer or a changed pattern must keep them passing. `python evals/run_evals.py privacy --languages en --cases evals/privacy_cases_en.jsonl` prints the metrics.

## Two common contributions

**A new recognizer.** Add the pattern (`hermie/gate/recognizers.py`: `SECRET_PATTERNS` for keys and credentials, or a Presidio recognizer in `build_analyzer`), add positive and near-miss cases to `evals/privacy_cases_en.jsonl` (or `privacy_cases.jsonl` for the Chinese engine), and keep `tests/test_evals.py` green. If the pattern also fires on code, extend `plausible` rather than lowering `presidio_threshold`.

**A new client or wire format.** Add a captured request to `tests/fixtures/requests` and a captured stream to `tests/fixtures/streams` (fake values only), add the keys that carry streamed text to `BUFFERED_KEYS` in `hermie/proxy/stream.py`, teach `walker.classify` where its tool results live if the shape is new, and add a live test to `tests/test_live.py` plus a row in the README's client table with the real result.

## Rules that are not negotiable

A pull request that weakens one of these will not be merged, however good the feature is.

- `send_upstream` only accepts a `CleanBody`, and a `CleanBody` is built only by `gate.certify_body` (after redaction) or `gate.empty_body` (body-less passthrough). No other path to the network.
- Fail closed: an exception in a detector or the judge means "sensitive".
- The receipt never holds a real value: no free-text field in `ReceiptLine`.
- No API key is stored, ever.
- Tests and fixtures use fake data only: 555-01xx phone numbers, `example.com` emails, card-network test numbers, keys that are obviously not real (no live-looking prefixes).
- English only in code and docs: comments, messages, docs and UI strings. The recognizers target Chinese-format data; that is logic, not text.

## Pull requests

1. Open an issue first for anything that changes what is scanned, what is sent or what is stored.
2. One concern per PR. Run `pytest -q` before pushing.
3. Say what the upstream can now see that it could not see before, even if the answer is "nothing".
