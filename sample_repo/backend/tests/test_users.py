"""Tests for the users service. These call sites also count as references to
``serialize_user`` and ``get_user`` (the index must find references in tests)."""

from __future__ import annotations

from app.users.models import User
from app.users.service import get_user, serialize_user


def test_serialize_user_shape() -> None:
    user = User(id="u1", name="Ada Lovelace", email="ada@example.com")
    result = serialize_user(user)
    assert result["id"] == "u1"
    assert result["name"] == "Ada Lovelace"


def test_get_user_missing() -> None:
    assert get_user("does-not-exist") is None


def test_get_user_present() -> None:
    result = get_user("u1")
    assert result is not None
    assert result["email"] == "ada@example.com"
