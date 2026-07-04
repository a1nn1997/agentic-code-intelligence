"""Interface contract for the orchestrator (agent loop).

A task is submitted, runs the bounded state machine, and ends in exactly one of
three terminal states. Progress is observable as an event stream (SSE at the
gateway, Phase 6). ``resume`` replays the journal: completed model calls return
cached, applied patches are not re-applied — so resuming a killed run never
double-charges or double-applies.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from acp.common.types import TaskMode, TaskState


class TaskRequest(BaseModel):
    """A unit of agent work, always scoped to an authenticated user + workspace."""

    user_id: str
    workspace_id: str
    instruction: str = Field(description="The user's intent — the trusted instruction channel")
    mode: TaskMode = TaskMode.APPLY
    max_tokens: int | None = Field(default=None, description="Per-task token budget override")
    max_steps: int | None = Field(default=None, description="Per-task step budget override")
    max_wall_seconds: int | None = Field(
        default=None, description="Per-task wall-clock deadline override"
    )


class TaskStatus(BaseModel):
    """A task's current state plus the metering needed for $/task reasoning."""

    task_id: str
    user_id: str
    workspace_id: str
    state: TaskState
    step_index: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    tool_calls: int = 0
    retrieval_bytes: int = 0
    sandbox_seconds: float = 0.0
    reason: str | None = Field(default=None, description="Set for GAVE_UP / BUDGET_EXHAUSTED")
    patch: str | None = Field(
        default=None,
        description="Combined patch JSON for dry_run tasks — not set for apply tasks",
    )


class TaskEvent(BaseModel):
    """One item on a task's progress stream (journal step -> SSE)."""

    task_id: str
    step_index: int
    kind: str = Field(description="StepKind value: plan | retrieve | edit | verify | repair")
    detail: str = ""


@runtime_checkable
class Orchestrator(Protocol):
    """Drive tasks through the bounded, replayable agent loop."""

    def submit(self, request: TaskRequest) -> TaskStatus:
        """Enqueue a task; returns its initial (PENDING) status."""
        ...

    def get_status(self, user_id: str, task_id: str) -> TaskStatus:
        """Current status of a task the user owns."""
        ...

    def resume(self, task_id: str) -> TaskStatus:
        """Replay the journal for a crash-interrupted task; effectively-once."""
        ...

    def stream_events(self, user_id: str, task_id: str) -> Iterator[TaskEvent]:
        """Yield progress events for a task the user owns."""
        ...
