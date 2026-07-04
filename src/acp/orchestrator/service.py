"""OrchestratorImpl — the Phase-4 concrete orchestrator over :class:`AgentLoop`.

Thin adapter that satisfies :class:`acp.orchestrator.interface.Orchestrator`:
it creates the task row, drives the hand-written loop to a terminal state, and
serves user-scoped status/trace. The consumer HTTP surface (auth, SSE) is
Phase 6; here the loop runs synchronously so the CLI and tests can single-step it.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from acp.common.errors import NotFound
from acp.common.types import TaskState
from acp.db import Database, JournalRepo, TasksRepo
from acp.orchestrator.interface import TaskEvent, TaskRequest, TaskStatus
from acp.orchestrator.loop import AgentLoop, LoopConfig
from acp.sandbox_client.interface import SandboxClient


class OrchestratorImpl:
    """Concrete orchestrator: create → run/resume → status/trace, user-scoped."""

    def __init__(
        self,
        db: Database,
        workspace_root: str | Path,
        sandbox: SandboxClient,
        *,
        default_token_budget: int,
        default_step_budget: int,
        default_wall_clock_seconds: int,
    ) -> None:
        from acp.model_gateway import build_model_gateway

        self._db = db
        self._tasks = TasksRepo(db)
        self._journal = JournalRepo(db)
        self._cfg = LoopConfig(
            db=db,
            workspace_root=workspace_root,
            gateway=build_model_gateway(),
            sandbox=sandbox,
        )
        self._defaults = (default_token_budget, default_step_budget, default_wall_clock_seconds)

    def submit(self, request: TaskRequest) -> TaskStatus:
        """Create a task row (PENDING) and run the loop to a terminal state.

        Returns the terminal status. Progress events are available via
        :meth:`stream_events` (the journal is the durable trace)."""
        tok, steps, wall = self._defaults
        task = self._tasks.create(
            request.user_id,
            request.workspace_id,
            request.instruction,
            token_budget=request.max_tokens or tok,
            step_budget=request.max_steps or steps,
            wall_clock_seconds=request.max_wall_seconds or wall,
            mode=request.mode,
        )
        loop = AgentLoop(self._cfg)
        return loop.run(task.id)

    def get_status(self, user_id: str, task_id: str) -> TaskStatus:
        task = self._tasks.get(user_id, task_id)
        if task is None:
            raise NotFound(f"task {task_id} not found for this user")
        return TaskStatus(
            task_id=task.id,
            user_id=task.user_id,
            workspace_id=task.workspace_id,
            state=TaskState(task.state),
            step_index=task.step_index,
            tokens_in=task.tokens_in,
            tokens_out=task.tokens_out,
            tool_calls=task.tool_calls,
            retrieval_bytes=task.retrieval_bytes,
            sandbox_seconds=task.sandbox_seconds,
            reason=task.reason,
        )
        # Note: patch for dry_run is set by AgentLoop._status, not here —
        # get_status is a lightweight read; the loop produces the canonical status.

    def resume(self, task_id: str) -> TaskStatus:
        """Replay the journal for a crash-interrupted task (effectively-once).

        Unscoped by design: resume is triggered server-side after a crash, never
        by a consumer. The loop's replay reuses cached model responses and does
        not re-apply patches, so this never double-charges or double-applies."""
        loop = AgentLoop(self._cfg)
        return loop.run(task_id)

    def stream_events(self, user_id: str, task_id: str) -> Iterator[TaskEvent]:
        """Yield the journal as progress events for a task the user owns."""
        if self._tasks.get(user_id, task_id) is None:
            raise NotFound(f"task {task_id} not found for this user")
        for entry in self._journal.get_trace(task_id):
            yield TaskEvent(
                task_id=task_id,
                step_index=entry.step_index,
                kind=entry.kind,
                detail=entry.payload_json,
            )
