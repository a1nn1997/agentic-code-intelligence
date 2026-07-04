"""Phase 6 + Phase 9 — API-only boundary tests (deepest-rigor clause (b)).

Asserts by ROUTE INVENTORY that there is NO consumer-reachable endpoint that:
  - queries the raw structural index,
  - shells/execs the sandbox,
  - reaches the model/model-gateway directly.

Phase 6 /v1 surface:
  POST  /v1/tasks
  GET   /v1/tasks/{task_id}
  GET   /v1/tasks/{task_id}/events

Phase 9 additions (UNPRIVILEGED CONSUMERS — expose only already-emitted
metering/journal data, auth-scoped per-user):
  GET /v1/dashboard/summary
  GET /v1/dashboard/runs
  GET /v1/dashboard/runs/{task_id}/trace

Static asset (no auth, no data access):
  GET /dashboard

Plus the operability surface: /healthz, /readyz, /metrics.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from acp.config.settings import Settings
from acp.gateway.app import create_app
from acp.sandbox_client.stub import StubSandboxClient

pytestmark = pytest.mark.integration

ALLOWED_PATHS = {
    "/healthz",
    "/readyz",
    "/metrics",
    "/docs",
    "/openapi.json",
    # /v1/tasks surface
    "/v1/tasks",
}

FORBIDDEN_SUBSTRINGS = [
    "index",
    "sandbox",
    "model",
    "exec",
    "shell",
    "retrieve",
    "symbol",
    "definition",
    "reference",
]


@pytest.fixture
def app_instance(settings: Settings):  # type: ignore[no-untyped-def]
    return create_app(settings=settings, sandbox=StubSandboxClient())


def test_route_inventory_no_index_or_sandbox_exposure(app_instance):  # type: ignore[no-untyped-def]
    """Route inventory: assert no route exposes the raw index, sandbox, or model.

    This is a STRUCTURAL test — it reads the actual FastAPI route list rather
    than testing HTTP responses — so it catches future route additions too.
    """
    from fastapi import FastAPI

    assert isinstance(app_instance, FastAPI)
    routes = [r.path for r in app_instance.routes if hasattr(r, "path")]

    for path in routes:
        path_lower = path.lower()
        for forbidden in FORBIDDEN_SUBSTRINGS:
            assert forbidden not in path_lower, (
                f"Route '{path}' contains forbidden substring '{forbidden}' — "
                "this would expose the index/sandbox/model to consumers"
            )


def test_v1_tasks_endpoint_requires_auth(settings: Settings):  # type: ignore[no-untyped-def]
    """Any /v1 endpoint requires auth; unauthenticated → 401 (not 200, not 500)."""
    app = create_app(settings=settings, sandbox=StubSandboxClient())
    client = TestClient(app, raise_server_exceptions=False)

    # POST /v1/tasks without auth
    resp = client.post("/v1/tasks", json={"workspace_id": "x", "instruction": "y"})
    assert resp.status_code == 401

    # GET /v1/tasks/{id} without auth
    resp = client.get("/v1/tasks/some_task")
    assert resp.status_code == 401

    # GET /v1/tasks/{id}/events without auth
    resp = client.get("/v1/tasks/some_task/events")
    assert resp.status_code == 401


def test_no_route_for_raw_index(settings: Settings):  # type: ignore[no-untyped-def]
    """Negative test: no /index or /symbols endpoint exists."""
    app = create_app(settings=settings, sandbox=StubSandboxClient())
    client = TestClient(app, raise_server_exceptions=False)
    for path in ["/index", "/symbols", "/sandbox/exec", "/model/complete"]:
        resp = client.get(path)
        assert resp.status_code in {404, 405}, f"Unexpected response for {path}: {resp.status_code}"


def test_dashboard_routes_require_auth(settings: Settings):  # type: ignore[no-untyped-def]
    """Phase 9: all /v1/dashboard/* routes enforce auth — unauthenticated → 401.

    The /dashboard static asset does NOT require auth (it is a public HTML page
    that the browser loads before the user enters a key); data endpoints do.
    """
    app = create_app(settings=settings, sandbox=StubSandboxClient())
    client = TestClient(app, raise_server_exceptions=False)

    for path in [
        "/v1/dashboard/summary",
        "/v1/dashboard/runs",
        "/v1/dashboard/runs/some_task/trace",
    ]:
        resp = client.get(path)
        assert resp.status_code == 401, f"Expected 401 for {path}, got {resp.status_code}"


def test_dashboard_static_asset_served(settings: Settings):  # type: ignore[no-untyped-def]
    """Phase 9: GET /dashboard returns the HTML bundle (200, text/html)."""
    app = create_app(settings=settings, sandbox=StubSandboxClient())
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")


def test_dashboard_html_no_browser_storage(settings: Settings):  # type: ignore[no-untyped-def]
    """Phase 9: the dashboard bundle must NOT call localStorage, sessionStorage,
    or IndexedDB — it is stateless by construction (multi-user ops surface).

    Checks for actual API call patterns (e.g. ``localStorage.setItem``),
    not just substring presence, so comments that mention the APIs by name to
    document their deliberate ABSENCE do not trigger a false positive.
    """
    import re

    app = create_app(settings=settings, sandbox=StubSandboxClient())
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    html = resp.text

    # Patterns that indicate actual API use (dot-access), not documentary mentions
    forbidden_patterns = [
        r"localStorage\s*\.",
        r"sessionStorage\s*\.",
        r"indexedDB\s*\.",
        r"openDatabase\s*\(",
    ]
    for pattern in forbidden_patterns:
        assert not re.search(pattern, html), (
            f"dashboard must not call browser storage API (found pattern: {pattern!r})"
        )


def test_dashboard_data_endpoints_not_privileged(app_instance):  # type: ignore[no-untyped-def]
    """Phase 9 deepest-rigor clause (a): route inventory confirms /v1/dashboard/*
    paths contain NONE of the forbidden index/sandbox/model substrings.

    Also asserts the routes ARE present (registered correctly).
    """

    routes = {r.path for r in app_instance.routes if hasattr(r, "path")}

    # Dashboard aggregate routes must be present
    assert "/v1/dashboard/summary" in routes
    assert "/v1/dashboard/runs" in routes
    assert "/v1/dashboard/runs/{task_id}/trace" in routes
    assert "/dashboard" in routes

    # None may contain a forbidden substring
    dashboard_routes = [p for p in routes if "dashboard" in p.lower()]
    for path in dashboard_routes:
        for forbidden in FORBIDDEN_SUBSTRINGS:
            assert forbidden not in path.lower(), (
                f"Dashboard route '{path}' contains forbidden substring '{forbidden}'"
            )


def test_dashboard_summary_cross_user_isolation(settings: Settings):  # type: ignore[no-untyped-def]
    """Phase 9 deepest-rigor clause (b): user A cannot see user B's data.

    A valid key for user A gets 200 on /v1/dashboard/summary (their own data).
    The returned user_id must match the authenticated user — not another user.
    A request with a valid key but for a task owned by user B returns 404 on
    the trace endpoint, not 200.
    """

    from acp.common.security import issue_api_key
    from acp.db.repositories import ApiKeysRepo, TasksRepo, UsersRepo, WorkspacesRepo
    from acp.gateway.app import create_app

    app = create_app(settings=settings, sandbox=StubSandboxClient())
    db = app.state.db

    users_repo = UsersRepo(db)
    keys_repo = ApiKeysRepo(db)
    workspaces_repo = WorkspacesRepo(db)
    tasks_repo = TasksRepo(db)

    # Create two independent users with keys
    user_a = users_repo.create()
    issued_a = issue_api_key()
    keys_repo.create(user_a.id, issued_a.prefix, issued_a.key_hash)

    user_b = users_repo.create()
    issued_b = issue_api_key()
    keys_repo.create(user_b.id, issued_b.prefix, issued_b.key_hash)

    # Create a workspace + task for user B
    ws_b = workspaces_repo.create(user_b.id, "https://example.com/repo.git")
    task_b = tasks_repo.create(
        user_b.id,
        ws_b.id,
        "rename_foo",
        token_budget=10000,
        step_budget=5,
        wall_clock_seconds=60,
    )

    client = TestClient(app, raise_server_exceptions=False)
    headers_a = {"Authorization": f"Bearer {issued_a.token}"}
    _ = issued_b  # user B's key exists but is not used to make requests here

    # User A's summary returns user A's id
    resp = client.get("/v1/dashboard/summary", headers=headers_a)
    assert resp.status_code == 200
    data = resp.json()
    assert data["user_id"] == user_a.id

    # User B's summary is NOT visible to user A (run count differs — A has 0)
    resp_a_runs = client.get("/v1/dashboard/runs", headers=headers_a)
    assert resp_a_runs.status_code == 200
    a_run_ids = {r["task_id"] for r in resp_a_runs.json()["runs"]}
    assert task_b.id not in a_run_ids, "user A must not see user B's tasks"

    # User A cannot fetch user B's trace — must be 404
    resp_trace = client.get(
        f"/v1/dashboard/runs/{task_b.id}/trace", headers=headers_a
    )
    assert resp_trace.status_code == 404, (
        f"Expected 404 for cross-user trace access, got {resp_trace.status_code}"
    )
