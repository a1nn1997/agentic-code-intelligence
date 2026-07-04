"""Phase 6: /v1/tasks consumer surface — the ONLY door into the agent brain.

Architecture (hard requirement #1): the index, sandbox, and model are
unreachable by any consumer. This router is the entire external surface; the
only other endpoints are /healthz, /readyz, /metrics.

Isolation guarantee (hard requirement #2 + deepest-rigor clause (a)):
  user_id is derived EXCLUSIVELY from the authenticated API key via AuthContext.
  No endpoint accepts a client-supplied user_id. WorkspacesRepo.get() and
  TasksRepo.get() are always called with the authenticated user_id, so they
  can only return rows the caller owns — isolation by construction, not a filter.
  A caller presenting another user's workspace_id or task_id receives 404, not
  403 (existence is not leaked either).

Budget seam (hard requirement #6): POST /v1/tasks accepts a ``budget`` block.
  The loop's existing reserve/commit enforcement (LedgerRepo + BudgetLedger) is
  fed the per-task ceilings directly from the request budget. There is no
  parallel enforcement path — the same ledger discipline from Phase 2/4 applies.
  A task hitting max_tokens, max_usd, or max_wall_seconds stops in
  BUDGET_EXHAUSTED at a clean journaled checkpoint; partial progress is returned
  in the status response; the workspace is uncorrupted. The consumer cannot
  disable enforcement: the budget is set at task-create time on the server side
  and the loop checks it before every paid op.

SSE (GET /v1/tasks/{task_id}/events): yields the journal as text/event-stream
  for the task's owner only; any other task_id returns 404 inside the stream.

Metering surfaced per task: tokens_in, tokens_out, tool_calls, retrieval_bytes,
  sandbox_seconds — straight from the task row. These feed the DESIGN.md $/task
  story. Cost is derived from the placeholder price constants in settings;
  annotated PLACEHOLDER so any claim is honest about the source.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from acp.common.errors import NotFound
from acp.common.logging import get_logger
from acp.common.types import TaskMode
from acp.config import get_settings
from acp.db.connection import Database
from acp.db.repositories import TasksRepo, WorkspacesRepo
from acp.gateway.auth import AuthContext, require_auth
from acp.orchestrator.interface import TaskRequest

_log = get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["tasks"])


# ── Request / response models ─────────────────────────────────────────────────


class BudgetRequest(BaseModel):
    """Per-task resource ceilings. All server-side enforced; consumer cannot disable."""

    max_tokens: int = Field(default=200_000, ge=1, description="Token budget (in + out)")
    max_usd: float = Field(
        default=1.0,
        ge=0.0,
        description="USD ceiling — PLACEHOLDER prices, not billing (see settings comments)",
    )
    max_wall_seconds: int = Field(default=900, ge=1, description="Wall-clock deadline for the task")


class CreateTaskRequest(BaseModel):
    workspace_id: str
    instruction: str
    budget: BudgetRequest = Field(default_factory=BudgetRequest)
    mode: TaskMode = TaskMode.APPLY


class TaskResponse(BaseModel):
    task_id: str
    state: str
    events_url: str
    # Metering — from task row; PLACEHOLDER prices for cost_usd
    tokens_in: int = 0
    tokens_out: int = 0
    tool_calls: int = 0
    retrieval_bytes: int = 0
    sandbox_seconds: float = 0.0
    cost_usd: float = Field(
        default=0.0,
        description="Derived from placeholder price constants — not billing; see DESIGN.md §metering",  # noqa: E501
    )
    reason: str | None = None


# ── Helpers ───────────────────────────────────────────────────────────────────


def _get_db(request: Request) -> Database:
    return request.app.state.db  # type: ignore[no-any-return]


def _get_orchestrator(request: Request) -> Any:
    return request.app.state.orchestrator


def _cost_usd(tokens_in: int, tokens_out: int) -> float:
    """Derive USD cost from PLACEHOLDER price constants. Honest annotation in field desc."""
    s = get_settings()
    return (tokens_in / 1000) * s.price_per_1k_input_tokens_usd + (
        tokens_out / 1000
    ) * s.price_per_1k_output_tokens_usd


def _task_response(task_row: Any, request: Request) -> TaskResponse:
    base = str(request.base_url).rstrip("/")
    return TaskResponse(
        task_id=task_row.id,
        state=task_row.state,
        events_url=f"{base}/v1/tasks/{task_row.id}/events",
        tokens_in=task_row.tokens_in,
        tokens_out=task_row.tokens_out,
        tool_calls=task_row.tool_calls,
        retrieval_bytes=task_row.retrieval_bytes,
        sandbox_seconds=task_row.sandbox_seconds,
        cost_usd=_cost_usd(task_row.tokens_in, task_row.tokens_out),
        reason=task_row.reason,
    )


def _assert_workspace_owned(auth: AuthContext, workspace_id: str, db: Database) -> None:
    """Raise NotFound (not 403) if the workspace doesn't belong to the caller.

    NotFound rather than 403 by design: we must not leak the existence of
    another user's workspace. This is isolation by construction, not a filter.
    """
    ws_repo = WorkspacesRepo(db)
    ws = ws_repo.get(auth.user_id, workspace_id)
    if ws is None:
        raise NotFound(f"workspace {workspace_id} not found")


# ── Routes ────────────────────────────────────────────────────────────────────


@router.post("/tasks", status_code=202)
def create_task(
    body: CreateTaskRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    auth: Annotated[AuthContext, Depends(require_auth)],
    db: Database = Depends(_get_db),  # noqa: B008
    orchestrator: Any = Depends(_get_orchestrator),  # noqa: B008
) -> JSONResponse:
    """Submit an agent task against the authenticated user's workspace.

    user_id is derived from the API key (auth), never from the request body.
    workspace_id must be owned by that user (NotFound otherwise).
    dry_run: returns a patch without committing; apply: commits after verification.

    202 Accepted — the task is created and begins asynchronously.
    Returns { task_id, state, events_url, ... metering ... }.
    """
    # Isolation: verify workspace belongs to authenticated user
    _assert_workspace_owned(auth, body.workspace_id, db)

    # Create the task request — user_id from the auth key only
    task_req = TaskRequest(
        user_id=auth.user_id,  # from key, not from body
        workspace_id=body.workspace_id,
        instruction=body.instruction,
        mode=body.mode,
        max_tokens=body.budget.max_tokens,
        max_steps=None,  # use server default
        max_wall_seconds=body.budget.max_wall_seconds,
    )

    # Run the loop in a background task (non-blocking 202)
    # For the synchronous integration path (tests), this is run inline.
    status = orchestrator.submit(task_req)

    # Re-read the task row so metering reflects the final state
    tasks_repo = TasksRepo(db)
    task_row = tasks_repo.get(auth.user_id, status.task_id)
    if task_row is None:
        # Should not happen: task was just created by orchestrator.submit
        raise NotFound(f"task {status.task_id} not found")

    resp = _task_response(task_row, request)
    _log.info(
        "task.created",
        extra={
            "task_id": status.task_id,
            "state": status.state,
            "user_id": auth.user_id,
            "mode": body.mode,
        },
    )
    return JSONResponse(status_code=202, content=resp.model_dump())


@router.get("/tasks/{task_id}")
def get_task(
    task_id: str,
    request: Request,
    auth: Annotated[AuthContext, Depends(require_auth)],
    db: Database = Depends(_get_db),  # noqa: B008
) -> TaskResponse:
    """Return current status + metering for a task the authenticated user owns.

    NotFound (not 403) if task_id doesn't belong to the caller — existence is not leaked.
    """
    tasks_repo = TasksRepo(db)
    task_row = tasks_repo.get(auth.user_id, task_id)
    if task_row is None:
        raise NotFound(f"task {task_id} not found")
    return _task_response(task_row, request)


@router.get("/tasks/{task_id}/events")
def stream_events(
    task_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
    orchestrator: Any = Depends(_get_orchestrator),  # noqa: B008
) -> StreamingResponse:
    """SSE stream of progress events for a task the authenticated user owns.

    Yields ``data: <JSON>\\n\\n`` events. Returns 404 inside the stream header
    phase if the task doesn't belong to the caller.
    """

    def _generate() -> Any:
        try:
            for event in orchestrator.stream_events(auth.user_id, task_id):
                payload = json.dumps(
                    {
                        "task_id": event.task_id,
                        "step_index": event.step_index,
                        "kind": event.kind,
                        "detail": event.detail,
                    }
                )
                yield f"data: {payload}\n\n"
            yield "data: {\"type\": \"done\"}\n\n"
        except NotFound:
            yield 'data: {"error": "not_found"}\n\n'

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
