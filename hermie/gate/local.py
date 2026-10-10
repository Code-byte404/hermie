"""Local rewriter: a local Ollama chat model rewrites a held message so it keeps the request and loses the sensitive
specifics. The text never leaves the machine here; the caller rescans the result before anything is sent."""
from __future__ import annotations

import httpx

from ..config import Config

LOCAL_PREFIX = "[rewritten locally] "

LOCAL_INSTRUCTION = (
    "Rewrite the user's message so it can be sent to an outside AI service. Keep the request, the task and the "
    "intent exactly as the user meant them. Replace every concrete personal, financial, legal, health or company "
    "detail (names, numbers, amounts, dates, addresses, identifiers, keys, source code secrets) with a general "
    "description of what it is, and never repeat such a value. Answer with the rewritten message only, no preface."
)


class LocalRewriter:
    def __init__(self, config: Config, client: httpx.AsyncClient | None = None):
        _, _, model = (config.local_model or "").partition(":")
        self.config = config
        self.model = model
        self.client = client or httpx.AsyncClient()

    async def rewrite(self, text: str) -> str | None:
        """The rewritten text, or None for any failure (too long, no server, timeout, unusable reply, empty)."""
        if len(text) > self.config.local_max_chars:
            return None
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": LOCAL_INSTRUCTION}, {"role": "user", "content": text}],
            "think": False,
            "stream": False,
        }
        try:
            resp = await self.client.post(f"{self.config.ollama_url.rstrip('/')}/api/chat", json=body,
                                          timeout=self.config.local_timeout_s)
            resp.raise_for_status()
            out = resp.json()["message"]["content"]
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return None
        return (out.strip() or None) if isinstance(out, str) else None


def make_local(config: Config) -> LocalRewriter | None:
    return LocalRewriter(config) if config.local_model else None
