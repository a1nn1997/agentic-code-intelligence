"""HTTP API for the /users endpoint.

First cross-file caller of ``serialize_user``.
"""

from __future__ import annotations

from app.users.models import UserId
from app.users.repository import list_user_rows
from app.users.service import get_user, serialize_user


def list_users() -> list[dict[str, object]]:
    """GET /users — list all users."""
    return [serialize_user(u) for u in list_user_rows()]


def get_user_endpoint(user_id: UserId) -> dict[str, object] | None:
    """GET /users/{user_id} — fetch one user."""
    return get_user(user_id)
