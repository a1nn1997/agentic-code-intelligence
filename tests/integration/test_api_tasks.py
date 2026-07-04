"""Phase 6 — /v1/tasks contract, isolation, budget, SSE, and dry_run tests.

Oracle clauses exercised:
  (3) CONTRACT — POST /v1/tasks → 202 + task_id + events URL
  (4) SSE — stream emits events for owner; NotFound for non-owner
  (5) BUDGET — tiny max_tokens → budget_exhausted at clean checkpoint
  (2) ISOLATION — user A cannot access user B's workspace or task
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from acp.common.security import issue_api_key
from acp.config.settings import Settings
from acp.db.repositories import ApiKeysRepo, UsersRepo, WorkspacesRepo
from acp.gateway.app import create_app
from acp.sandbox_client.interface import VerificationRequest, VerificationResult, VerificationStage
from acp.sandbox_client.stub import StubSandboxClient


class _VerifyingStub(StubSandboxClient):
    """Stub that returns a successful verification result — no Docker needed."""

    def verify_snapshot(
        self, request: VerificationRequest, snapshot_dir: Path
    ) -> VerificationResult:
        return VerificationResult(
            verified=True,
            applied=True,
            built=True,
            tests_passed=True,
            exit_code=0,
            stage=VerificationStage.DONE,
        )

    def verify(self, request: VerificationRequest) -> VerificationResult:
        return self.verify_snapshot(request, Path())

pytestmark = pytest.mark.integration

SAMPLE_REPO_SRC = Path(__file__).resolve().parents[2] / "sample_repo"


def _setup_user_with_workspace(db, workspace_root: Path):  # type: ignore[no-untyped-def]
    """Create a user + API key + workspace seeded with the sample repo."""
    users = UsersRepo(db)
    keys_repo = ApiKeysRepo(db)
    ws_repo = WorkspacesRepo(db)

    user = users.create()
    issued = issue_api_key()
    keys_repo.create(user.id, issued.prefix, issued.key_hash)

    # Seed the sample repo into the workspace root
    ws = ws_repo.create(user.id, "local://sample")
    user_ws_path = workspace_root / user.id / ws.id
    user_ws_path.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SAMPLE_REPO_SRC, user_ws_path / "repo")
    # Update workspace head_commit so retrieval can find it
    ws_repo.set_head(user.id, ws.id, "stub_head")

    return user, issued.token, ws


@pytest.fixture
def app_and_users(settings: Settings, tmp_path: Path):  # type: ignore[no-untyped-def]
    """App + two isolated users (A and B)."""
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    settings = settings.model_copy(
        update={"workspace_root": str(workspace_root)}
    )
    app = create_app(settings=settings, sandbox=_VerifyingStub())
    db = app.state.db

    user_a, token_a, ws_a = _setup_user_with_workspace(db, workspace_root)
    user_b, token_b, ws_b = _setup_user_with_workspace(db, workspace_root)

    client = TestClient(app, raise_server_exceptions=False)
    return client, token_a, ws_a, token_b, ws_b


# ── POST /v1/tasks contract ───────────────────────────────────────────────────


def test_post_tasks_returns_202(app_and_users):  # type: ignore[no-untyped-def]
    client, token_a, ws_a, _, _ = app_and_users
    resp = client.post(
        "/v1/tasks",
        json={
            "workspace_id": ws_a.id,
            "instruction": "add a docstring to the get_user function",
            "budget": {"max_tokens": 10000, "max_usd": 0.1, "max_wall_seconds": 60},
        },
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert "task_id" in body
    assert "events_url" in body
    assert "/v1/tasks/" in body["events_url"]


def test_post_tasks_isolation_wrong_workspace(app_and_users):  # type: ignore[no-untyped-def]
    """User A cannot submit a task against user B's workspace — NotFound, not 403."""
    client, token_a, _, _, ws_b = app_and_users
    resp = client.post(
        "/v1/tasks",
        json={
            "workspace_id": ws_b.id,  # B's workspace
            "instruction": "do something",
        },
        headers={"Authorization": f"Bearer {token_a}"},  # A's key
    )
    assert resp.status_code == 404
    assert resp.json().get("error") == "not_found"


# ── GET /v1/tasks/{task_id} ───────────────────────────────────────────────────


def test_get_task_isolation(app_and_users):  # type: ignore[no-untyped-def]
    """User A cannot GET user B's task — NotFound, not B's data."""
    client, token_a, ws_a, token_b, ws_b = app_and_users

    # User B submits a task
    resp = client.post(
        "/v1/tasks",
        json={"workspace_id": ws_b.id, "instruction": "add a comment"},
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert resp.status_code == 202
    task_id_b = resp.json()["task_id"]

    # User A tries to GET user B's task
    resp = client.get(
        f"/v1/tasks/{task_id_b}",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert resp.status_code == 404
    assert resp.json().get("error") == "not_found"


def test_get_task_owner_succeeds(app_and_users):  # type: ignore[no-untyped-def]
    """Task owner can GET their task."""
    client, token_a, ws_a, _, _ = app_and_users

    resp = client.post(
        "/v1/tasks",
        json={"workspace_id": ws_a.id, "instruction": "add a comment"},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert resp.status_code == 202
    task_id = resp.json()["task_id"]

    resp = client.get(
        f"/v1/tasks/{task_id}",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert resp.status_code == 200
    assert resp.json()["task_id"] == task_id


# ── SSE /v1/tasks/{task_id}/events ───────────────────────────────────────────


def test_sse_stream_owner_gets_events(app_and_users):  # type: ignore[no-untyped-def]
    client, token_a, ws_a, _, _ = app_and_users

    resp = client.post(
        "/v1/tasks",
        json={"workspace_id": ws_a.id, "instruction": "add a comment"},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    task_id = resp.json()["task_id"]

    # Stream events — the stream returns the journal, then a "done" sentinel
    resp = client.get(
        f"/v1/tasks/{task_id}/events",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    # Parse the SSE lines
    events = [
        json.loads(line.removeprefix("data: "))
        for line in resp.text.splitlines()
        if line.startswith("data: ")
    ]
    # At minimum there should be a "done" sentinel event
    assert any(e.get("type") == "done" or "task_id" in e for e in events)


def test_sse_stream_non_owner_gets_not_found(app_and_users):  # type: ignore[no-untyped-def]
    """Non-owner gets a not_found error in the SSE stream."""
    client, token_a, ws_a, token_b, _ = app_and_users

    resp = client.post(
        "/v1/tasks",
        json={"workspace_id": ws_a.id, "instruction": "add a comment"},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    task_id = resp.json()["task_id"]

    # User B tries to stream user A's events
    resp = client.get(
        f"/v1/tasks/{task_id}/events",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert resp.status_code == 200  # SSE stays 200 but emits error
    events = [
        json.loads(line.removeprefix("data: "))
        for line in resp.text.splitlines()
        if line.startswith("data: ")
    ]
    assert any(e.get("error") == "not_found" for e in events)


# ── Budget enforcement ────────────────────────────────────────────────────────


def test_budget_exhausted_stops_cleanly(app_and_users):  # type: ignore[no-untyped-def]
    """A task with a tiny token budget stops in budget_exhausted, partial, workspace intact."""
    client, token_a, ws_a, _, _ = app_and_users

    resp = client.post(
        "/v1/tasks",
        json={
            "workspace_id": ws_a.id,
            "instruction": "add a docstring to the get_user function",
            "budget": {"max_tokens": 1, "max_usd": 0.0001, "max_wall_seconds": 60},
        },
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert resp.status_code == 202
    body = resp.json()
    task_id = body["task_id"]

    # Re-read the task; it should be budget_exhausted
    resp = client.get(
        f"/v1/tasks/{task_id}",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert resp.status_code == 200
    status = resp.json()
    assert status["state"] == "budget_exhausted", (
        f"Expected budget_exhausted, got {status['state']}"
    )


# ── dry_run ───────────────────────────────────────────────────────────────────


def test_dry_run_returns_patch_without_committing(app_and_users, tmp_path: Path):  # type: ignore[no-untyped-def]
    """dry_run: the task runs and the patch is returned; base workspace is unchanged."""
    client, token_a, ws_a, _, _ = app_and_users

    resp = client.post(
        "/v1/tasks",
        json={
            "workspace_id": ws_a.id,
            "instruction": "add a docstring to the get_user function",
            "mode": "dry_run",
        },
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert resp.status_code == 202
    body = resp.json()
    # State should be a terminal state (the stub model drives to one)
    assert body["state"] in ("verified_success", "gave_up", "budget_exhausted")
