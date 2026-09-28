"""Observability data stays on this machine. The audit log holds no original text; the outbound log
holds only certified clean content."""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class JsonlLog:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def write(self, record: dict[str, Any]) -> None:
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **record}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock, open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError:
            log.exception("Failed to write log: %s", self.path)

    def tail(self, n: int = 50) -> list[dict]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()[-n:]
        return [json.loads(x) for x in lines if x.strip()]


class AuditLog(JsonlLog):
    """Time, input hash, route and reasons, signals, execution backend, outbound count, latency.
    No original text."""

    def task(self, *, text: str, route: str, reasons: list[str], signals: dict, backend: str,
             outbound_count: int, latency_s: float, tainted: bool, mode: str) -> None:
        self.write({"input_sha256": sha256(text), "route": route, "reasons": reasons, "signals": signals,
                    "backend": backend, "outbound_count": outbound_count, "latency_s": round(latency_s, 3),
                    "tainted": tainted, "mode": mode})
