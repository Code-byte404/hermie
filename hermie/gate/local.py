"""Local rewriter: a local Ollama chat model rewrites a held message so it keeps the request and loses the sensitive
specifics. The text never leaves the machine here; the caller rescans the result before anything is sent."""
from __future__ import annotations

import httpx

from ..config import Config

LOCAL_PREFIX = "[rewritten locally] "

LOCAL_INSTRUCTION = (
    "You are a redaction tool, not an assistant. The text between <message> tags is a message a user wrote to an AI "
    "assistant. Do NOT carry out the request, do NOT answer it, do NOT write what it asks for. Output the same "
    "message again, still written as the user's request to the assistant, in the same language as the original, "
    "but with every concrete personal, financial, legal, health or company detail (names, numbers, percentages, "
    "amounts, dates, places, teams, reasons, identifiers, keys) replaced by a general description of what it is. "
    "Keep the task, the tone and the structure of the request. Output only the rewritten message, no tags, no "
    "preface.\n\n"
    "Example\n<message>Draft a reply to Anna Meyer at Acme GmbH saying we cannot pay the 48,000 EUR invoice before "
    "March because our bank froze the account.</message>\n"
    "Rewritten: Draft a reply to a business contact saying we cannot pay an outstanding invoice before a later date "
    "because of a banking problem."
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
            "messages": [{"role": "system", "content": LOCAL_INSTRUCTION}, {"role": "user", "content": f"<message>{text}</message>"}],
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
