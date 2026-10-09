"""Optional contextual privacy judge: a local Ollama chat model (`ollama:MODEL`) or a fine-tuned Laya decision model
(`laya:PATH`, through laya-mlx, Apple Silicon only)."""
from __future__ import annotations

import json
import threading
from pathlib import Path

import httpx

from ..config import Config

CONTEXTUAL_PRIVACY_QUESTION = (
    "Does this material contain sensitive information that should not be sent to an external third party? "
    "This includes: an individual's health/financial/family/legal situation, unpublished internal company "
    "information (layoffs, mergers and acquisitions, financial results, customer lists, source code, secret "
    "keys), or any description that identifies a specific person. Answer no if it is only public common "
    "knowledge or a general question."
)

_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string", "enum": ["yes", "no"]}},
    "required": ["answer"],
}


class JudgeError(Exception):
    """The judge could not produce a usable answer."""


class ContextualJudge:
    def __init__(self, config: Config, client: httpx.Client | None = None):
        prefix, _, model = (config.judge or "").partition(":")
        if prefix != "ollama" or not model:
            raise ValueError(f"unsupported judge {config.judge!r}; expected 'ollama:MODEL'")
        self.config = config
        self.model = model
        self.client = client or httpx.Client()

    def probability(self, text: str) -> float:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": CONTEXTUAL_PRIVACY_QUESTION + ' Answer with JSON {"answer": "yes"|"no"}.'},
                {"role": "user", "content": text[:8000]},
            ],
            "format": _SCHEMA,
            "think": False,
            "stream": False,
        }
        try:
            resp = self.client.post(
                f"{self.config.ollama_url.rstrip('/')}/api/chat", json=body, timeout=self.config.judge_timeout_s
            )
            resp.raise_for_status()
            answer = json.loads(resp.json()["message"]["content"])["answer"]
        except httpx.HTTPError as e:
            raise JudgeError(f"judge request failed: {type(e).__name__}") from e
        except (ValueError, KeyError, TypeError) as e:
            raise JudgeError(f"judge returned an unusable reply: {type(e).__name__}") from e
        if answer not in ("yes", "no"):
            raise JudgeError("judge answer is not yes/no")
        return 1.0 if answer == "yes" else 0.0

    def is_sensitive(self, text: str) -> bool:
        return self.probability(text) >= self.config.judge_threshold


# The question the Laya checkpoints were fine-tuned on (evals/judge_data/taxonomy.py holds the same string; a test keeps them equal).
LAYA_QUESTION = ("Does this text contain private information about a specific person, or non-public company "
                 "information, that should not be sent to an outside AI service?")


class LayaJudge:
    """A fine-tuned Laya checkpoint answering one yes/no question in a single forward pass (tens of milliseconds, no
    generated text). The model reads about 1,000 tokens, so a longer text is cut into WINDOW-character windows and the
    highest score wins; at most MAX_WINDOWS windows are scored, which bounds the latency on very large texts."""

    WINDOW = 4000
    MAX_WINDOWS = 8
    _ID = "private"

    def __init__(self, config: Config):
        prefix, _, path = (config.judge or "").partition(":")
        if prefix != "laya" or not path:
            raise ValueError(f"unsupported judge {config.judge!r}; expected 'laya:PATH'")
        try:
            import laya_mlx
        except ImportError as e:
            raise ValueError("judge laya:PATH needs laya-mlx (pip install laya-mlx; Apple Silicon only)") from e
        try:
            self._agent = laya_mlx.load(str(Path(path).expanduser()))
        except Exception as e:   # a bad path or a broken checkpoint: fail at startup, not on the first request
            raise ValueError(f"could not load the Laya checkpoint {path!r}: {type(e).__name__}") from e
        self.config = config
        self._questions = {self._ID: {"type": "noul", "instructions": LAYA_QUESTION}}
        self._lock = threading.Lock()   # the scan runs in worker threads; one forward pass at a time

    def _score(self, text: str) -> float:
        with self._lock:
            return float(self._agent.predict(text, self._questions)["answers"][self._ID]["noul"])

    def probability(self, text: str) -> float:
        windows = [text[i:i + self.WINDOW] for i in range(0, len(text), self.WINDOW)][: self.MAX_WINDOWS] or [""]
        try:
            best = max(self._score(w) for w in windows)
        except Exception as e:   # never carry the text or the model's message into the error
            raise JudgeError(f"judge failed: {type(e).__name__}") from e
        if not 0.0 <= best <= 1.0:   # also false for NaN
            raise JudgeError("judge returned a score outside 0..1")
        return best

    def is_sensitive(self, text: str) -> bool:
        return self.probability(text) >= self.config.judge_threshold


def make_judge(config: Config):
    prefix = (config.judge or "").partition(":")[0]
    if prefix == "ollama":
        return ContextualJudge(config)
    if prefix == "laya":
        return LayaJudge(config)
    raise ValueError(f"unsupported judge {config.judge!r}; expected 'ollama:MODEL' or 'laya:PATH'")
