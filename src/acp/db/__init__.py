"""Typed data-access layer over SQLite (WAL mode).

The rule (locked decision): **no raw SQL in business modules**. Every query
lives in :mod:`acp.db.repositories`; modules receive a :class:`Database` and
call typed methods. SQLite runs in WAL mode with ``BEGIN IMMEDIATE`` for writes,
which the plan designates as the serialization point for the journal and ledger
— the guarantee that concurrent tasks can't corrupt the append-only tables.
"""

from acp.db.connection import Database, connect, init_db
from acp.db.repositories import (
    ApiKeysRepo,
    ArtifactsRepo,
    JournalRepo,
    LedgerRepo,
    TasksRepo,
    UsersRepo,
    WorkspacesRepo,
)

__all__ = [
    "Database",
    "connect",
    "init_db",
    "UsersRepo",
    "ApiKeysRepo",
    "WorkspacesRepo",
    "TasksRepo",
    "JournalRepo",
    "LedgerRepo",
    "ArtifactsRepo",
]
