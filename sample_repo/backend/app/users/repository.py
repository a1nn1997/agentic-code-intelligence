"""In-memory user repository (stand-in for a real datastore)."""

from __future__ import annotations

from app.users.models import User, UserId

_USERS: dict[str, User] = {
    "u1": User(id="u1", name="Ada Lovelace", email="ada@example.com"),
    "u2": User(id="u2", name="Alan Turing", email="alan@example.com"),
}


def find_user(user_id: UserId) -> User | None:
    """Return the user with ``user_id`` or None."""
    return _USERS.get(user_id)


def list_user_rows() -> list[User]:
    """Return all users."""
    return list(_USERS.values())
