"""Data-access layer: CRUD, user-scoped isolation, journal idempotency,
ledger balance, and the append-only invariant. These back the resume /
no-double-charge / cross-user-isolation guarantees, so they are tested directly.
"""

from __future__ import annotations

import sqlite3

import pytest

from acp.common.security import issue_api_key
from acp.common.types import LedgerEntryKind, StepKind, TaskState
from acp.db.connection import Database
from acp.db.repositories import (
    ApiKeysRepo,
    JournalRepo,
    LedgerRepo,
    TasksRepo,
    UsersRepo,
    WorkspacesRepo,
)

pytestmark = pytest.mark.unit


def test_api_key_stored_hashed_only(db: Database) -> None:
    UsersRepo(db).create("user_a")
    issued = issue_api_key()
    ApiKeysRepo(db).create("user_a", issued.prefix, issued.key_hash)

    # Inspect the raw row: no column holds the raw secret.
    row = db.conn.execute("SELECT * FROM api_keys;").fetchone()
    _, raw_secret = issued.token.split(".")
    assert row["key_hash"] == issued.key_hash
    assert raw_secret not in dict(row).values()


def test_workspace_reads_are_user_scoped(db: Database) -> None:
    users = UsersRepo(db)
    users.create("user_a")
    users.create("user_b")
    ws_repo = WorkspacesRepo(db)
    ws_a = ws_repo.create("user_a", source="repo://a")

    # Owner sees it; the other user cannot address it at all.
    assert ws_repo.get("user_a", ws_a.id) is not None
    assert ws_repo.get("user_b", ws_a.id) is None
    assert ws_repo.list("user_b") == []


def test_journal_append_is_idempotent(db: Database) -> None:
    users, ws, tasks = UsersRepo(db), WorkspacesRepo(db), TasksRepo(db)
    users.create("user_a")
    w = ws.create("user_a", "repo://a")
    t = tasks.create(
        "user_a", w.id, "do a thing",
        token_budget=1000, step_budget=10, wall_clock_seconds=60,
    )
    journal = JournalRepo(db)

    entry1, created1 = journal.append(
        t.id, 0, StepKind.PLAN, "idem-0", {"model": "resp-A"}
    )
    # Re-executing the same step (crash-resume) must NOT create a new row and
    # must return the ORIGINAL payload — no double effect, no double charge.
    entry2, created2 = journal.append(
        t.id, 0, StepKind.PLAN, "idem-0", {"model": "resp-B-should-be-ignored"}
    )
    assert created1 is True
    assert created2 is False
    assert entry1.id == entry2.id
    assert '"model":"resp-A"' in entry2.payload_json
    assert len(journal.get_trace(t.id)) == 1


def test_journal_is_append_only_no_update(db: Database) -> None:
    users, ws, tasks = UsersRepo(db), WorkspacesRepo(db), TasksRepo(db)
    users.create("user_a")
    w = ws.create("user_a", "repo://a")
    t = tasks.create(
        "user_a", w.id, "x", token_budget=1, step_budget=1, wall_clock_seconds=1
    )
    JournalRepo(db).append(t.id, 0, StepKind.PLAN, "k", {})
    # The DB trigger must reject any UPDATE to a written journal row.
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute("UPDATE journal SET kind='verify' WHERE task_id = ?;", (t.id,))


def test_ledger_balance_reserve_commit_release(db: Database) -> None:
    users, ws, tasks = UsersRepo(db), WorkspacesRepo(db), TasksRepo(db)
    users.create("user_a")
    w = ws.create("user_a", "repo://a")
    t = tasks.create(
        "user_a", w.id, "x", token_budget=1000, step_budget=10, wall_clock_seconds=60
    )
    ledger = LedgerRepo(db)
    scope = f"task:{t.id}"

    ledger.append(scope, LedgerEntryKind.RESERVE, tokens=500, task_id=t.id)
    ledger.append(scope, LedgerEntryKind.RESERVE, tokens=200, task_id=t.id)
    ledger.append(scope, LedgerEntryKind.RELEASE, tokens=200, task_id=t.id)
    ledger.append(scope, LedgerEntryKind.COMMIT, tokens=450, task_id=t.id)

    assert ledger.reserved_tokens(scope) == 500  # 500 + 200 - 200
    assert ledger.spent_tokens(scope) == 450


def test_task_state_transition(db: Database) -> None:
    users, ws, tasks = UsersRepo(db), WorkspacesRepo(db), TasksRepo(db)
    users.create("user_a")
    w = ws.create("user_a", "repo://a")
    t = tasks.create(
        "user_a", w.id, "x", token_budget=1, step_budget=1, wall_clock_seconds=1
    )
    tasks.set_state(t.id, TaskState.VERIFIED_SUCCESS)
    refreshed = tasks.get("user_a", t.id)
    assert refreshed is not None
    assert refreshed.state == TaskState.VERIFIED_SUCCESS.value
