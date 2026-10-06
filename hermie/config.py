"""Proxy configuration: config.toml < HERMIE_* env < explicit overrides."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Literal

_TUPLE_FIELDS = ("languages", "deny_words", "allow_values", "allow_paths")


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8787
    mode: Literal["enforce", "observe"] = "enforce"
    languages: tuple[str, ...] = ("en",)
    judge: str | None = None
    ollama_url: str = "http://localhost:11434"
    judge_threshold: float = 0.5
    judge_timeout_s: float = 20.0
    presidio_threshold: float = 0.5
    images: Literal["withhold", "pass"] = "withhold"
    deny_words: tuple[str, ...] = ()
    allow_values: tuple[str, ...] = ()
    allow_paths: tuple[str, ...] = ()
    custom_upstream: str | None = None
    bodies_keep_mb: int = 500
    bodies: bool = True
    hold_timeout_s: int = 60
    cache_mb: int = 256
    data_dir: Path | None = field(default_factory=lambda: Path("~/.hermie").expanduser())

    def __post_init__(self) -> None:
        for name in _TUPLE_FIELDS:
            value = getattr(self, name)
            if isinstance(value, str):
                value = _split(value)
            setattr(self, name, tuple(value))
        if self.data_dir is not None:
            self.data_dir = Path(self.data_dir).expanduser()

    def _under(self, name: str) -> Path | None:
        return None if self.data_dir is None else self.data_dir / name

    @property
    def mapping_path(self) -> Path | None:
        return self._under("mapping.json")

    @property
    def allowed_path(self) -> Path | None:
        return self._under("allowed.json")

    @property
    def receipt_path(self) -> Path | None:
        return self._under("receipts.jsonl")

    @property
    def outbound_dir(self) -> Path | None:
        return self._under("outbound")

    @property
    def config_path(self) -> Path | None:
        return self._under("config.toml")

    @classmethod
    def load(cls, path: Path | None = None, **overrides) -> "Config":
        values: dict = {}
        if path is None:
            default = cls().config_path
            if default is not None and default.is_file():
                path = default
        if path is not None:
            with open(Path(path).expanduser(), "rb") as f:
                values.update(tomllib.load(f))
        for f in fields(cls):
            raw = os.environ.get("HERMIE_" + f.name.upper())
            if raw is not None:
                values[f.name] = _parse_env(f.name, raw, getattr(cls(), f.name))
        values.update(overrides)
        known = {f.name for f in fields(cls)}
        unknown = set(values) - known
        if unknown:
            raise ValueError(f"unknown config keys: {sorted(unknown)}")
        return cls(**values)


def _split(raw: str) -> list[str]:
    return [p.strip() for p in raw.split(",") if p.strip()]


def _parse_env(name: str, raw: str, default):
    if name in _TUPLE_FIELDS:
        return tuple(_split(raw))
    if name == "judge" or name == "custom_upstream":
        return raw or None
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    return raw
