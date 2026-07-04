"""Phase 9 — dashboard read-only aggregate endpoints.

These routes are UNPRIVILEGED CONSUMERS: they read ONLY already-emitted
metering and journal data through the same auth stack as every other /v1 route.
They expose NO path to the raw index, sandbox, or model.

Route inventory (Phase 9 additions):
  GET /v1/dashboard/summary   — KPI aggregate for the authenticated user
  GET /v1/dashboard/runs      — paginated run table (tasks with metering)
  GET /v1/dashboard/runs/{task_id}/trace — journal trace for owned task

Auth + isolation:
  All three routes inject ``require_auth`` → user_id from the verified key.
  TasksRepo.list/get and JournalRepo.get_trace are user-scoped; a caller sees
  ONLY their own data.  A foreign task_id returns 404 (NotFound), not 403 —
  existence is not leaked, identical to Phase 6 behaviour.

No browser storage:
  These endpoints serve JSON; the HTML bundle that calls them holds no state
  — reopening the dashboard reconstructs everything from these calls.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from acp.common.errors import NotFound
from acp.db.connection import Database
from acp.db.repositories import JournalRepo, TasksRepo
from acp.gateway.auth import AuthContext, require_auth

router = APIRouter(prefix="/v1/dashboard", tags=["dashboard"])


def _get_db(request: Request) -> Database:
    return request.app.state.db  # type: ignore[no-any-return]


# ── helpers ───────────────────────────────────────────────────────────────────

_COST_PER_TOKEN_IN = 0.000003
_COST_PER_TOKEN_OUT = 0.000015


def _cost_usd(tokens_in: int, tokens_out: int) -> float:
    return tokens_in * _COST_PER_TOKEN_IN + tokens_out * _COST_PER_TOKEN_OUT


def _task_to_dict(task: Any) -> dict[str, Any]:
    cost = _cost_usd(task.tokens_in, task.tokens_out)
    return {
        "task_id": task.id,
        "workspace_id": task.workspace_id,
        "state": task.state,
        "mode": task.mode,
        "tokens_in": task.tokens_in,
        "tokens_out": task.tokens_out,
        "tool_calls": task.tool_calls,
        "retrieval_bytes": task.retrieval_bytes,
        "sandbox_seconds": task.sandbox_seconds,
        "cost_usd": round(cost, 6),
        "token_budget": task.token_budget,
        "reason": task.reason,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


# ── endpoints ─────────────────────────────────────────────────────────────────


@router.get("/summary")
def dashboard_summary(
    auth: Annotated[AuthContext, Depends(require_auth)],
    db: Database = Depends(_get_db),  # noqa: B008
) -> dict[str, Any]:
    """KPI aggregate for the authenticated user — data the platform already emits.

    Returns counts by terminal state, total tokens + cost, sandbox stats, and
    the counts of injection-defended and secret-redacted events from the journal.
    Reads ONLY tasks + journal rows owned by the caller; no index/sandbox/model access.
    """
    tasks_repo = TasksRepo(db)
    tasks = tasks_repo.list(auth.user_id)

    by_state: dict[str, int] = {}
    total_tokens_in = 0
    total_tokens_out = 0
    total_cost = 0.0
    total_sandbox_seconds = 0.0
    sandbox_pass = 0
    sandbox_fail = 0

    journal_repo = JournalRepo(db)
    injections_defended = 0
    secrets_redacted = 0

    for task in tasks:
        state = task.state
        by_state[state] = by_state.get(state, 0) + 1
        total_tokens_in += task.tokens_in
        total_tokens_out += task.tokens_out
        total_cost += _cost_usd(task.tokens_in, task.tokens_out)
        total_sandbox_seconds += task.sandbox_seconds

        # Count journal events for security metrics
        trace = journal_repo.get_trace(task.id)
        for entry in trace:
            try:
                payload = json.loads(entry.payload_json)
            except (json.JSONDecodeError, TypeError):
                payload = {}
            kind = entry.kind
            if kind == "injection_defended" or payload.get("injection_detected"):
                injections_defended += 1
            if kind == "secret_redacted" or payload.get("secrets_redacted"):
                redacted_count = payload.get("secrets_redacted", 1)
                secrets_redacted += redacted_count if isinstance(redacted_count, int) else 1
            if kind == "sandbox_result":
                if payload.get("exit_code", -1) == 0:
                    sandbox_pass += 1
                else:
                    sandbox_fail += 1

    return {
        "user_id": auth.user_id,
        "tasks_total": len(tasks),
        "tasks_by_state": by_state,
        "injections_defended": injections_defended,
        "secrets_redacted": secrets_redacted,
        "sandbox_pass": sandbox_pass,
        "sandbox_fail": sandbox_fail,
        "total_tokens_in": total_tokens_in,
        "total_tokens_out": total_tokens_out,
        "total_cost_usd": round(total_cost, 6),
        "total_sandbox_seconds": round(total_sandbox_seconds, 2),
    }


@router.get("/runs")
def dashboard_runs(
    auth: Annotated[AuthContext, Depends(require_auth)],
    limit: int = 50,
    db: Database = Depends(_get_db),  # noqa: B008
) -> dict[str, Any]:
    """Paginated run table for the authenticated user.

    Returns the most-recent ``limit`` tasks with metering fields.
    User-scoped: TasksRepo.list() pins to caller's user_id.
    """
    tasks_repo = TasksRepo(db)
    tasks = tasks_repo.list(auth.user_id, limit=min(limit, 200))
    return {
        "runs": [_task_to_dict(t) for t in tasks],
        "count": len(tasks),
    }


@router.get("/runs/{task_id}/trace")
def dashboard_trace(
    task_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
    db: Database = Depends(_get_db),  # noqa: B008
) -> dict[str, Any]:
    """Journal trace for a task owned by the authenticated user.

    Returns journal entries rendered as the plan→retrieve→edit→verify→repair
    state machine with per-step tokens/bytes/sandbox-seconds.
    Returns 404 for foreign task_ids — existence is not leaked.
    """
    tasks_repo = TasksRepo(db)
    task = tasks_repo.get(auth.user_id, task_id)
    if task is None:
        raise NotFound(f"task {task_id} not found")

    journal_repo = JournalRepo(db)
    entries = journal_repo.get_trace(task_id)

    steps = []
    for entry in entries:
        try:
            payload = json.loads(entry.payload_json)
        except (json.JSONDecodeError, TypeError):
            payload = {}
        steps.append(
            {
                "step_index": entry.step_index,
                "kind": entry.kind,
                "tokens_in": payload.get("tokens_in", 0),
                "tokens_out": payload.get("tokens_out", 0),
                "retrieval_bytes": payload.get("retrieval_bytes", 0),
                "sandbox_seconds": payload.get("sandbox_seconds", 0.0),
                "result": payload.get("result"),
                "created_at": entry.created_at,
            }
        )

    return {
        "task": _task_to_dict(task),
        "steps": steps,
        "step_count": len(steps),
    }
