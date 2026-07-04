"""Reporting — CSV/JSON export of users.

Second cross-file caller of ``serialize_user`` (proves N>1 call sites across
files, which the multi-file rename in Phase 5 must all update).
"""

from __future__ import annotations

from app.users.repository import list_user_rows
from app.users.service import serialize_user


def export_users() -> list[dict[str, object]]:
    """Export every user as a serialized dict."""
    rows = list_user_rows()
    return [serialize_user(row) for row in rows]
