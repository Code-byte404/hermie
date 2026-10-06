"""Test fixtures."""
import pytest

from hermie.gate.recognizers import build_analyzer


@pytest.fixture(scope="session")
def analyzer():
    return build_analyzer(("en",))


@pytest.fixture(scope="session")
def analyzer_zh():
    pytest.importorskip("spacy")
    import spacy
    if "zh_core_web_sm" not in spacy.util.get_installed_models():
        pytest.skip("zh model not installed")
    return build_analyzer(("en", "zh"))
