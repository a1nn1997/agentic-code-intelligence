"""Smoke: the gateway boots and its operability endpoints answer. This is the
core of the Phase-0 oracle (`/healthz` + `/readyz` = 200), exercised in-process
via FastAPI's TestClient — no network, no keys.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from acp.config.settings import Settings
from acp.gateway.app import create_app

pytestmark = pytest.mark.smoke


@pytest.fixture
def client(settings: Settings) -> TestClient:
    # Inject a StubSandboxClient so the smoke test does not require Docker.
    # The /readyz seam uses the real DockerSandboxRunner in production;
    # this stub is the in-process-unit-test path that Phase 6 explicitly preserves.
    from acp.sandbox_client.stub import StubSandboxClient

    app = create_app(settings=settings, sandbox=StubSandboxClient())
    return TestClient(app)


def test_healthz_ok(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_readyz_ok_in_stub_mode(client: TestClient) -> None:
    resp = client.get("/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["checks"] == {"database": True, "sandbox": True}


def test_metrics_endpoint_serves_prometheus_text(client: TestClient) -> None:
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "acp_" in resp.text


def test_request_id_header_present(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.headers.get("x-request-id", "").startswith("req_")


def test_readyz_reports_not_ready_when_db_unreachable(settings: Settings) -> None:
    # Phase 6: DB is initialised at app startup. To test the /readyz DB-failure
    # path we close the underlying connection after creation so the SELECT 1 fails.
    from acp.sandbox_client.stub import StubSandboxClient

    app = create_app(settings=settings, sandbox=StubSandboxClient())
    # Close the DB connection so the readyz check finds it unreachable.
    app.state.db.close()
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["checks"]["database"] is False
