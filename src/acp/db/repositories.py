"""Typed repositories — the ONLY place SQL text lives.

Each repository takes a :class:`acp.db.connection.Database` and exposes typed
methods returning :mod:`acp.db.models`. Two invariants are load-bearing:

* **Journal append is idempotent** on ``(task_id, step_index)``. A resumed run
  that re-executes a step hits the UNIQUE constraint, we swallow the conflict,
  and return the *already-stored* entry — so replay never double-writes or
  double-charges.
* **Workspace/task reads are user-scoped.** ``get`` methods that serve consumer
  requests take ``user_id`` and filter on it; there is no unscoped getter on the
  consumer path, so one user cannot address another's data.
"""

from __future__ import annotations

import json
from typing import Any

from acp.common.errors import BudgetExceeded, IsolationViolation, NotFound
from acp.common.types import LedgerEntryKind, StepKind, TaskMode, TaskState, new_id
from acp.db.connection import Database
from acp.db.models import (
    ApiKey,
    Artifact,
    JournalEntry,
    LedgerEntry,
    Task,
    User,
    Workspace,
)


class UsersRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def create(self, user_id: str | None = None) -> User:
        uid = user_id or new_id("user")
        with self._db.immediate() as conn:
            conn.execute("INSERT INTO users (id) VALUES (?);", (uid,))
        return self.get(uid)  # type: ignore[return-value]

    def get(self, user_id: str) -> User | None:
        row = self._db.conn.execute("SELECT * FROM users WHERE id = ?;", (user_id,)).fetchone()
        return User.from_row(row) if row else None


class ApiKeysRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def create(
        self,
        user_id: str,
        key_prefix: str,
        key_hash: str,
        *,
        rate_limit_per_min: int = 60,
        daily_token_budget: int = 2_000_000,
    ) -> ApiKey:
        kid = new_id("key")
        with self._db.immediate() as conn:
            conn.execute(
                "INSERT INTO api_keys "
                "(id, user_id, key_prefix, key_hash, rate_limit_per_min, daily_token_budget) "
                "VALUES (?, ?, ?, ?, ?, ?);",
                (kid, user_id, key_prefix, key_hash, rate_limit_per_min, daily_token_budget),
            )
        row = self._db.conn.execute("SELECT * FROM api_keys WHERE id = ?;", (kid,)).fetchone()
        return ApiKey.from_row(row)

    def get_by_prefix(self, key_prefix: str) -> ApiKey | None:
        """Lookup by the non-secret prefix; caller then constant-time-verifies
        the secret against ``key_hash``."""
        row = self._db.conn.execute(
            "SELECT * FROM api_keys WHERE key_prefix = ? AND revoked = 0;", (key_prefix,)
        ).fetchone()
        return ApiKey.from_row(row) if row else None

    def count(self) -> int:
        row = self._db.conn.execute("SELECT COUNT(*) AS n FROM api_keys;").fetchone()
        return int(row["n"])


class WorkspacesRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def create(self, user_id: str, source: str, head_commit: str | None = None) -> Workspace:
        wid = new_id("ws")
        with self._db.immediate() as conn:
            conn.execute(
                "INSERT INTO workspaces (id, user_id, source, head_commit) VALUES (?, ?, ?, ?);",
                (wid, user_id, source, head_commit),
            )
        return self.get(user_id, wid)  # type: ignore[return-value]

    def get(self, user_id: str, workspace_id: str) -> Workspace | None:
        """User-scoped read — filters on user_id so it can only return rows the
        caller owns."""
        row = self._db.conn.execute(
            "SELECT * FROM workspaces WHERE id = ? AND user_id = ?;", (workspace_id, user_id)
        ).fetchone()
        return Workspace.from_row(row) if row else None

    def list(self, user_id: str) -> list[Workspace]:
        rows = self._db.conn.execute(
            "SELECT * FROM workspaces WHERE user_id = ? ORDER BY created_at;", (user_id,)
        ).fetchall()
        return [Workspace.from_row(r) for r in rows]

    def set_head(self, user_id: str, workspace_id: str, head_commit: str) -> None:
        """Record the indexed snapshot id. User-scoped: the WHERE clause pins the
        owner, so a write can only ever touch the caller's own workspace row."""
        with self._db.immediate() as conn:
            conn.execute(
                "UPDATE workspaces SET head_commit = ? WHERE id = ? AND user_id = ?;",
                (head_commit, workspace_id, user_id),
            )


class TasksRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def create(
        self,
        user_id: str,
        workspace_id: str,
        instruction: str,
        *,
        token_budget: int,
        step_budget: int,
        wall_clock_seconds: int,
        mode: TaskMode = TaskMode.APPLY,
    ) -> Task:
        tid = new_id("task")
        with self._db.immediate() as conn:
            conn.execute(
                "INSERT INTO tasks "
                "(id, user_id, workspace_id, state, mode, instruction, "
                " token_budget, step_budget, wall_clock_seconds) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);",
                (
                    tid,
                    user_id,
                    workspace_id,
                    TaskState.PENDING.value,
                    mode.value,
                    instruction,
                    token_budget,
                    step_budget,
                    wall_clock_seconds,
                ),
            )
        return self.get(user_id, tid)  # type: ignore[return-value]

    def get(self, user_id: str, task_id: str) -> Task | None:
        """User-scoped read for consumer requests."""
        row = self._db.conn.execute(
            "SELECT * FROM tasks WHERE id = ? AND user_id = ?;", (task_id, user_id)
        ).fetchone()
        return Task.from_row(row) if row else None

    def list(self, user_id: str, limit: int = 100) -> list[Task]:
        """User-scoped task list for the dashboard aggregate endpoint."""
        rows = self._db.conn.execute(
            "SELECT * FROM tasks WHERE user_id = ? ORDER BY created_at DESC LIMIT ?;",
            (user_id, limit),
        ).fetchall()
        return [Task.from_row(r) for r in rows]

    def get_for_resume(self, task_id: str) -> Task | None:
        """Unscoped read used ONLY by the internal resume path (no consumer
        reaches this — resume is triggered server-side after a crash)."""
        row = self._db.conn.execute("SELECT * FROM tasks WHERE id = ?;", (task_id,)).fetchone()
        return Task.from_row(row) if row else None

    def set_state(self, task_id: str, state: TaskState, reason: str | None = None) -> None:
        with self._db.immediate() as conn:
            conn.execute(
                "UPDATE tasks SET state = ?, reason = ?, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?;",
                (state.value, reason, task_id),
            )


class JournalRepo:
    """Append-only journal access. This is the resume/replay substrate."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def append(
        self,
        task_id: str,
        step_index: int,
        kind: StepKind,
        idempotency_key: str,
        payload: dict[str, Any] | None = None,
    ) -> tuple[JournalEntry, bool]:
        """Idempotent append. Returns ``(entry, created)``.

        If ``(task_id, step_index)`` already exists, the INSERT is a no-op and we
        return the pre-existing entry with ``created=False`` — the caller then
        knows to reuse the cached payload instead of re-executing side effects.
        """
        payload_json = json.dumps(payload or {}, separators=(",", ":"), sort_keys=True)
        with self._db.immediate() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO journal "
                "(task_id, step_index, kind, idempotency_key, payload_json) "
                "VALUES (?, ?, ?, ?, ?);",
                (task_id, step_index, kind.value, idempotency_key, payload_json),
            )
            created = cur.rowcount == 1
        entry = self.get_step(task_id, step_index)
        if entry is None:  # pragma: no cover — insert-or-existing guarantees a row
            raise NotFound(f"journal step ({task_id}, {step_index}) vanished after append")
        return entry, created

    def get_step(self, task_id: str, step_index: int) -> JournalEntry | None:
        row = self._db.conn.execute(
            "SELECT * FROM journal WHERE task_id = ? AND step_index = ?;",
            (task_id, step_index),
        ).fetchone()
        return JournalEntry.from_row(row) if row else None

    def get_trace(self, task_id: str) -> list[JournalEntry]:
        """The full ordered journal for a task — the per-run trace (`make trace`)."""
        rows = self._db.conn.execute(
            "SELECT * FROM journal WHERE task_id = ? ORDER BY step_index;", (task_id,)
        ).fetchall()
        return [JournalEntry.from_row(r) for r in rows]


class LedgerRepo:
    """Append-only budget ledger. Balance is a signed sum over a scope."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def append(
        self,
        scope: str,
        kind: LedgerEntryKind,
        *,
        tokens: int = 0,
        cost_usd: float = 0.0,
        task_id: str | None = None,
        step_index: int | None = None,
        note: str | None = None,
    ) -> LedgerEntry:
        with self._db.immediate() as conn:
            cur = conn.execute(
                "INSERT INTO budget_ledger "
                "(scope, kind, task_id, step_index, tokens, cost_usd, note) "
                "VALUES (?, ?, ?, ?, ?, ?, ?);",
                (scope, kind.value, task_id, step_index, tokens, cost_usd, note),
            )
            rowid = cur.lastrowid
        row = self._db.conn.execute(
            "SELECT * FROM budget_ledger WHERE id = ?;", (rowid,)
        ).fetchone()
        return LedgerEntry.from_row(row)

    def spent_tokens(self, scope: str) -> int:
        """Committed token spend for a scope (COMMIT entries only)."""
        row = self._db.conn.execute(
            "SELECT COALESCE(SUM(tokens), 0) AS n FROM budget_ledger "
            "WHERE scope = ? AND kind = ?;",
            (scope, LedgerEntryKind.COMMIT.value),
        ).fetchone()
        return int(row["n"])

    def reserved_tokens(self, scope: str) -> int:
        """Net outstanding reservations (reserve minus release), for pre-op checks."""
        row = self._db.conn.execute(
            "SELECT COALESCE(SUM(CASE kind WHEN ? THEN tokens WHEN ? THEN -tokens ELSE 0 END), 0) "
            "AS n FROM budget_ledger WHERE scope = ?;",
            (LedgerEntryKind.RESERVE.value, LedgerEntryKind.RELEASE.value, scope),
        ).fetchone()
        return int(row["n"])

    def charge_atomic(
        self,
        scope: str,
        tokens: int,
        budget_tokens: int,
        *,
        task_id: str | None = None,
        step_index: int | None = None,
        note: str | None = None,
    ) -> None:
        """Check the budget and reserve→commit→release ``tokens`` under ONE
        ``BEGIN IMMEDIATE`` — the atomic charge that closes the B-3 TOCTOU.

        The old flow read ``remaining_tokens()`` (two SELECTs, each its own
        read transaction) and *then* wrote three ledger rows in three *separate*
        write transactions. Two concurrent retrievals on one scope could both
        read the pre-charge balance, both pass the ceiling check, and both
        commit — overshooting the budget. Here the balance read and the writes
        happen inside a single write transaction: ``BEGIN IMMEDIATE`` takes the
        write lock up front, so a second charger blocks until the first has
        committed and then reads the *post-charge* balance. The ceiling can
        never be overshot.

        On refusal (``tokens`` would exceed the remaining budget) this raises
        :class:`BudgetExceeded` and, because the raise happens inside the
        ``immediate()`` body, the transaction rolls back — **nothing is written**
        (no-write-on-refusal preserved). The committed-spend post-condition is
        identical to the old ``_charge``: ``spent`` rises by exactly ``tokens``
        and net ``reserved`` returns to 0.
        """
        with self._db.immediate() as conn:
            spent_row = conn.execute(
                "SELECT COALESCE(SUM(tokens), 0) AS n FROM budget_ledger "
                "WHERE scope = ? AND kind = ?;",
                (scope, LedgerEntryKind.COMMIT.value),
            ).fetchone()
            reserved_row = conn.execute(
                "SELECT COALESCE(SUM(CASE kind WHEN ? THEN tokens WHEN ? THEN -tokens "
                "ELSE 0 END), 0) AS n FROM budget_ledger WHERE scope = ?;",
                (LedgerEntryKind.RESERVE.value, LedgerEntryKind.RELEASE.value, scope),
            ).fetchone()
            remaining = budget_tokens - int(spent_row["n"]) - int(reserved_row["n"])
            if tokens > remaining:
                # Raising here rolls back the (empty) transaction: no ledger write.
                raise BudgetExceeded(
                    f"retrieval would cost {tokens} tokens; "
                    f"only {remaining} remain for scope {scope}"
                )
            for kind in (
                LedgerEntryKind.RESERVE,
                LedgerEntryKind.COMMIT,
                LedgerEntryKind.RELEASE,
            ):
                conn.execute(
                    "INSERT INTO budget_ledger "
                    "(scope, kind, task_id, step_index, tokens, cost_usd, note) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?);",
                    (scope, kind.value, task_id, step_index, tokens, 0.0, note),
                )


class ArtifactsRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def create(
        self, task_id: str, kind: str, content_hash: str, path: str | None = None
    ) -> Artifact:
        aid = new_id("art")
        with self._db.immediate() as conn:
            conn.execute(
                "INSERT INTO artifacts (id, task_id, kind, content_hash, path) "
                "VALUES (?, ?, ?, ?, ?);",
                (aid, task_id, kind, content_hash, path),
            )
        row = self._db.conn.execute("SELECT * FROM artifacts WHERE id = ?;", (aid,)).fetchone()
        return Artifact.from_row(row)

    def list_for_task(self, task_id: str) -> list[Artifact]:
        rows = self._db.conn.execute(
            "SELECT * FROM artifacts WHERE task_id = ? ORDER BY created_at;", (task_id,)
        ).fetchall()
        return [Artifact.from_row(r) for r in rows]


def assert_owned(user_id: str, row_user_id: str) -> None:
    """Defense-in-depth: raise if an object's owner isn't the caller.

    The scoped getters make cross-user reads unrepresentable; this is the belt
    to that suspenders, used where an id crosses a trust boundary.
    """
    if user_id != row_user_id:
        raise IsolationViolation("caller does not own the requested resource")
