"""Test fixtures."""
import pytest

from hermie.config import Settings
from hermie.privacy import build_analyzer


@pytest.fixture(scope="session")
def analyzer():
    return build_analyzer(Settings(custom_keywords=("ProjectCodenameA",)))
