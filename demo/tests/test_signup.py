import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
pytest.importorskip("flask")

from app import create_app, validate_phone  # noqa: E402


def test_dashed_phone_is_valid():
    assert validate_phone("555-010-0101")


def test_plain_phone_is_valid():
    assert validate_phone("5550100101")


def test_signup_accepts_dashed_phone():
    client = create_app().test_client()
    r = client.post("/signup", json={"email": "ada.lovelace@example.com", "phone": "555-010-0101"})
    assert r.status_code == 201
