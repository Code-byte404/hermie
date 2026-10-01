"""Local models must not think unless WORKER_THINKING is on: Qwen3-class models think by default, which costs
minutes per reply on a laptop, and every ad-hoc local agent used to forget the switch."""
import ast
from pathlib import Path

from hermie.agents import ModelFactory

_LOCAL = {"executor", "compressor", "reviewer"}   # ModelFactory methods that return the Ollama model


def test_local_settings_turn_thinking_off(settings):
    settings.worker_thinking = False
    s = ModelFactory(settings).local_settings()
    assert s["extra_body"] == {"reasoning_effort": "none"} and s["thinking"] is False
    settings.worker_thinking = True
    assert "extra_body" not in ModelFactory(settings).local_settings()


def test_every_local_agent_uses_local_settings():
    missing = []
    for path in Path(__file__).resolve().parents[1].joinpath("hermie").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Agent" and node.args):
                continue
            first = node.args[0]
            if isinstance(first, ast.Call) and getattr(first.func, "attr", None) in _LOCAL:
                kw = {k.arg: k.value for k in node.keywords}
                value = kw.get("model_settings")
                ok = value is not None and "local_settings" in ast.unparse(value)
                if not ok:
                    missing.append(f"{path.name}:{node.lineno}")
    assert not missing, f"local agents without models.local_settings(): {missing}"
