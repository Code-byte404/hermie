"""Optional contextual privacy judge over a local Ollama model."""
from __future__ import annotations

import json

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
