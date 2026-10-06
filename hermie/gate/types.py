from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

_GATE_TOKEN = object()   # held only by the gate package


class Origin(Enum):
    USER = "user"
    TOOL = "tool"
    ASSISTANT = "assistant"
    BINARY = "binary"
    OTHER = "other"


@dataclass
class Finding:
    entity: str
    start: int
    end: int
    score: float


class CleanBody:
    """A request body the gate has redacted and allowed to leave this machine. Only gate.certify_body builds it."""
    __slots__ = ("data",)

    def __init__(self, data: bytes, _token: object = None):
        if _token is not _GATE_TOKEN:
            raise PermissionError("CleanBody can only be created by the gate")
        self.data = data

    def __repr__(self) -> str:
        return f"CleanBody(len={len(self.data)})"


@dataclass
class ScanResult:
    text: str
    findings: list[Finding]
    judged: bool
    sensitive: bool
    reason: str
    new_mapping: dict[str, str]
    hash: str
    size: int = 0   # original text length, for cache accounting
    cached: bool = False   # True on a copy returned for a cache hit
