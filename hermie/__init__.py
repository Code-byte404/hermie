"""Local-first hybrid agent framework: Ollama local execution + cloud planning (DeepSeek, OpenAI, Anthropic or any OpenAI-compatible endpoint), privacy gate + Seatbelt sandbox."""
from .config import RunMode, Settings

__all__ = ["RunMode", "Settings", "Hermie", "TaskResult"]


def __getattr__(name):  # lazy import so that `python -m hermie --help` does not load pydantic-ai either
    if name in ("Hermie", "TaskResult"):
        from . import core
        return getattr(core, name)
    raise AttributeError(name)
