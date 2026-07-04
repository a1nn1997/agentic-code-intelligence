"""Domain enums and identifier helpers shared by the schema and the module
interfaces.

Kept dependency-free (stdlib + a tiny id helper) so both ``db`` and every
domain module can import these without creating a cycle. The task terminal
states here are the single source of truth referenced by the agent loop
(Phase 4) and the ``tasks`` table.
"""

from __future__ import annotations

import secrets
import uuid
from enum import StrEnum


class TaskState(StrEnum):
    """Lifecycle of a task.

    The three *terminal* states (VERIFIED_SUCCESS, GAVE_UP, BUDGET_EXHAUSTED)
    are the only ways a run may end — enforced by the agent loop in Phase 4.
    The rest are in-flight states used for resume/replay.
    """

    PENDING = "pending"
    RUNNING = "running"
    # --- terminal states (exactly three) ---
    VERIFIED_SUCCESS = "verified_success"
    GAVE_UP = "gave_up"
    BUDGET_EXHAUSTED = "budget_exhausted"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_STATES


_TERMINAL_STATES = frozenset(
    {TaskState.VERIFIED_SUCCESS, TaskState.GAVE_UP, TaskState.BUDGET_EXHAUSTED}
)


class TaskMode(StrEnum):
    """Whether a task applies changes or only plans them."""

    APPLY = "apply"
    DRY_RUN = "dry_run"


class StepKind(StrEnum):
    """The agent-loop state machine's step types.

    Journalled per step; the dashboard (Phase 9) renders a run's journal as this
    plan -> retrieve -> edit -> verify -> repair sequence.
    """

    PLAN = "plan"
    RETRIEVE = "retrieve"
    EDIT = "edit"
    VERIFY = "verify"
    REPAIR = "repair"


class LedgerEntryKind(StrEnum):
    """Direction of a budget-ledger entry. The ledger is append-only; a balance
    is the signed sum of entries for a scope."""

    RESERVE = "reserve"
    COMMIT = "commit"
    RELEASE = "release"


def new_id(prefix: str) -> str:
    """A sortable-ish, prefixed unique id, e.g. ``task_a1b2c3...``.

    UUID4 hex keeps ids opaque and collision-free without a central sequence.
    The prefix makes ids self-describing in logs and the journal.
    """
    return f"{prefix}_{uuid.uuid4().hex}"


def new_secret(nbytes: int = 32) -> str:
    """A URL-safe random secret for API-key generation (never stored raw)."""
    return secrets.token_urlsafe(nbytes)
