"""The cloud planner / cloud direct model can come from DeepSeek (default), OpenAI, Anthropic or any
OpenAI-compatible endpoint; the privacy path is the same for all of them."""
import pytest

from hermie.agents import ModelFactory
from hermie.config import Settings

from .conftest import FakeJudge, Script, text, tool


def _settings(**kw) -> Settings:
    base = dict(cloud_provider="deepseek", cloud_api_key="k", cloud_base_url="", cloud_model="m", cloud_plan_model="p")
    base.update(kw)
    return Settings(**base)


def test_deepseek_models_by_default():
    m = ModelFactory(_settings())
    assert m.cloud(planning=False).model_name == "m" and m.cloud(planning=True).model_name == "p"
    assert m.planner().system == "deepseek" and m.cloud_available


def test_openai_provider():
    m = ModelFactory(_settings(cloud_provider="openai", cloud_model="gpt-x"))
    model = m.cloud(planning=False)
    assert model.system == "openai" and model.model_name == "gpt-x"


def test_openai_compatible_provider_uses_base_url():
    m = ModelFactory(_settings(cloud_provider="openai-compatible", cloud_base_url="https://openrouter.ai/api/v1"))
    model = m.planner()
    assert model.model_name == "p" and model._provider.base_url.startswith("https://openrouter.ai/api/v1")


def test_openai_compatible_needs_base_url():
    m = ModelFactory(_settings(cloud_provider="openai-compatible"))
    with pytest.raises(RuntimeError, match="CLOUD_BASE_URL"):
        m.planner()


def test_anthropic_provider():
    pytest.importorskip("anthropic", reason="optional extra: pip install 'hermie[anthropic]'")
    m = ModelFactory(_settings(cloud_provider="anthropic", cloud_plan_model="claude-opus-5-5"))
    model = m.planner()
    assert model.system == "anthropic" and model.model_name == "claude-opus-5-5"


def test_missing_key_and_model_are_reported():
    with pytest.raises(RuntimeError, match="CLOUD_API_KEY"):
        ModelFactory(_settings(cloud_api_key="")).planner()
    with pytest.raises(RuntimeError, match="CLOUD_PLAN_MODEL"):  # openai has no default model names
        ModelFactory(_settings(cloud_provider="openai", cloud_plan_model="")).planner()
    assert _settings(cloud_plan_model="").cloud_plan_model == "deepseek-v4-pro"  # deepseek does
    assert not ModelFactory(_settings(cloud_api_key="")).cloud_available


async def test_backend_names_the_provider(make_agent):
    cloud = Script([text("Three-month plan")], name="cloud")
    agent = make_agent(FakeJudge(task="planning", needs_ws=False), cloud=cloud, cloud_provider="anthropic")
    r = await agent.run("Draft a three-month learning plan for Rust")
    assert r.route == "cloud" and r.backend == "anthropic"


async def test_plan_backend_names_the_provider(make_agent):
    planner = Script([tool("delegate", step="Create hello.py"), text("Done")], name="planner")
    agent = make_agent(FakeJudge(task="planning"), planner=planner, cloud_provider="openai")
    r = await agent.run("Create a hello world script")
    assert r.route == "plan" and r.backend == "openai-plan+ollama"
