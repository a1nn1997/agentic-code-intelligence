"""Phase-7 e2e test: full path through the HTTP layer.

index → POST /v1/tasks → real sandbox verify → verified_success.

This test drives the COMPLETE stack via the HTTP API (not internal Python
calls) and asserts that the terminal state is verified_success backed by a
real sandbox VerificationResult.  It is the only test in the suite that goes
all the way through the HTTP boundary from the outside-in.

Gated on Docker because the sandbox must run a real container.  When Docker is
unavailable the test is auto-skipped (same pattern as test_agent_loop_docker.py).

Oracle:
  1. POST /v1/tasks returns 202 + task_id
  2. GET /v1/tasks/{id} eventually returns state=verified_success
  3. The journal (via agentctl trace) contains a VERIFY step
  4. An artifact was recorded for the task (confirming the edit applied)
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from acp.common.security import issue_api_key
from acp.config.settings import Settings
from acp.db import ArtifactsRepo, JournalRepo
from acp.db.repositories import ApiKeysRepo, UsersRepo
from acp.gateway.app import create_app
from acp.sandbox_client import build_sandbox_client
from acp.workspace import WorkspaceServiceImpl

pytestmark = [pytest.mark.integration, pytest.mark.docker, pytest.mark.e2e]

SAMPLE_REPO_SRC = Path(__file__).resolve().parents[2] / "sample_repo"


@pytest.fixture(autouse=True)
def _require_docker() -> None:
    """Gate the module on the real sandbox image.

    A8: when ``ACP_REQUIRE_DOCKER=1`` and the daemon is up but the image is
    missing, FAIL loudly rather than skip — a buildable-but-skipped real path is
    a false green for the assignment's central proof."""
    from tests.docker_gate import _docker_daemon_up, docker_required

    from acp.sandbox_client import build_sandbox_client as _build

    client = _build()
    if client.healthy():
        return
    if docker_required():
        if _docker_daemon_up():
            pytest.fail(
                "acp-sandbox image absent but ACP_REQUIRE_DOCKER=1 and the Docker "
                "daemon is up — the e2e real-sandbox proof was demanded. Build via "
                "`make sandbox-build`. Refusing to report green without it.",
                pytrace=False,
            )
        pytest.fail(
            "ACP_REQUIRE_DOCKER=1 but the Docker daemon is unreachable — cannot "
            "run the required e2e real-sandbox proof.",
            pytrace=False,
        )
    pytest.skip("Docker sandbox not healthy — skipping e2e tests")


@pytest.fixture
def e2e_app(tmp_path: Path, settings: Settings):  # type: ignore[no-untyped-def]
    """Full app with real Docker sandbox client, seeded workspace."""
    ws_root = tmp_path / "workspaces"
    ws_root.mkdir()
    settings = settings.model_copy(update={"workspace_root": str(ws_root)})

    # Build with the real Docker sandbox (not a stub).
    sandbox = build_sandbox_client()
    app = create_app(settings=settings, sandbox=sandbox)
    db = app.state.db

    # Seed a user + API key.
    users = UsersRepo(db)
    keys = ApiKeysRepo(db)
    user = users.create()
    issued = issue_api_key()
    keys.create(user.id, issued.prefix, issued.key_hash)

    # Create + index a workspace from the sample repo.
    svc = WorkspaceServiceImpl(db, str(ws_root))
    ref = svc.create_workspace(user.id, str(SAMPLE_REPO_SRC))
    svc.build_index(user.id, ref.workspace_id)

    client = TestClient(app, raise_server_exceptions=False)
    return client, issued.token, user.id, ref.workspace_id, db, str(ws_root)


def test_e2e_http_index_task_sandbox_verified(e2e_app) -> None:  # type: ignore[no-untyped-def]
    """End-to-end: index already built → POST /v1/tasks → poll → verified_success.

    The agent runs against the sample repo with the stub model (no API key)
    and the REAL Docker sandbox.  The sandbox applies the patch, builds, and
    runs tests — returning a genuine VerificationResult with verified=True.

    Oracle assertions (all concrete, none self-reported):
      1. HTTP 202 on task creation
      2. terminal state == verified_success from GET /v1/tasks/{id}
      3. Journal contains a VERIFY step (sandbox was actually called)
      4. An artifact is recorded (edit was applied)
    """
    client, token, user_id, workspace_id, db, ws_root = e2e_app

    # 1. POST /v1/tasks through the HTTP layer.
    resp = client.post(
        "/v1/tasks",
        json={
            "workspace_id": workspace_id,
            "instruction": "add a passing test target_symbol=serialize_user",
            "budget": {"max_tokens": 200_000, "max_wall_seconds": 120},
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 202, f"expected 202, got {resp.status_code}: {resp.text}"
    task_id = resp.json()["task_id"]
    assert task_id, "task_id missing from 202 response"

    # 2. Poll GET /v1/tasks/{id} until terminal (up to 90 s).
    terminal_states = {"verified_success", "gave_up", "budget_exhausted"}
    deadline = time.monotonic() + 90
    state = None
    while time.monotonic() < deadline:
        r = client.get(f"/v1/tasks/{task_id}", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, f"GET /v1/tasks/{task_id} returned {r.status_code}"
        state = r.json().get("state")
        if state in terminal_states:
            break
        time.sleep(1)

    assert state == "verified_success", (
        f"expected verified_success, got {state!r} — "
        "task may have timed out or the sandbox produced an unexpected result"
    )

    # 3. Journal must contain a VERIFY step (sandbox was actually called).
    trace = list(JournalRepo(db).get_trace(task_id))
    kinds = [e.kind for e in trace]
    assert "verify" in kinds, f"no VERIFY step in journal — sandbox never called; kinds={kinds}"

    # 4. An artifact must be recorded (edit was applied, not just claimed).
    arts = ArtifactsRepo(db).list_for_task(task_id)
    assert arts, "no artifact recorded — edit was never applied"
