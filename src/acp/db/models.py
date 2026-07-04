"""Row models for the data-access layer.

Plain pydantic models mirroring the schema. Repositories return these, never
raw ``sqlite3.Row`` objects, so business modules get typed, validated data and
the SQL shape stays contained in :mod:`acp.db.repositories`.
"""

from __future__ import annotations

import sqlite3
from typing import Self

from pydantic import BaseModel


class _Row(BaseModel):
    """Base with a helper to build from a ``sqlite3.Row``."""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Self:
        return cls(**dict(row))


class User(_Row):
    id: str
    created_at: str


class ApiKey(_Row):
    id: str
    user_id: str
    key_prefix: str
    key_hash: str
    rate_limit_per_min: int
    daily_token_budget: int
    revoked: int
    created_at: str


class Workspace(_Row):
    id: str
    user_id: str
    source: str
    head_commit: str | None
    created_at: str


class Task(_Row):
    id: str
    user_id: str
    workspace_id: str
    state: str
    mode: str
    instruction: str
    reason: str | None
    step_index: int
    token_budget: int
    step_budget: int
    wall_clock_seconds: int
    tokens_in: int
    tokens_out: int
    tool_calls: int
    retrieval_bytes: int
    sandbox_seconds: float
    created_at: str
    updated_at: str


class JournalEntry(_Row):
    id: int
    task_id: str
    step_index: int
    kind: str
    idempotency_key: str
    payload_json: str
    created_at: str


class LedgerEntry(_Row):
    id: int
    scope: str
    kind: str
    task_id: str | None
    step_index: int | None
    tokens: int
    cost_usd: float
    note: str | None
    created_at: str


class Artifact(_Row):
    id: str
    task_id: str
    kind: str
    content_hash: str
    path: str | None
    created_at: str
