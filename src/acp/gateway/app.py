"""FastAPI application factory — Phase 9 wired.

Phase 9 additions over Phase 6:
* ``GET /dashboard`` — serves the static vanilla HTML/CSS/JS observability
  dashboard bundle.  It is a static asset route; it does NOT reach the index,
  sandbox, or model.
* ``/v1/dashboard/*`` router — three thin read-only auth-scoped aggregate
  endpoints (summary, runs, runs/{id}/trace) that expose ONLY already-emitted
  metering/journal data.  They are unprivileged consumers, identical in auth
  discipline to every other /v1 route.

Phase 6 notes preserved:
* ``app.state.db`` — the Database handle shared by auth and all /v1 routes.
* ``app.state.orchestrator`` — the OrchestratorImpl wired with the real (or
  injected-stub) sandbox; the /v1 tasks router calls it directly.
* ``/readyz`` now checks the REAL sandbox runner's ``healthy()`` (built from
  settings via ``build_sandbox_client``). The StubSandboxClient lives only in
  unit tests that explicitly inject it via the ``sandbox`` kwarg.
* The ``/v1/tasks`` router is included. It is the only door into the agent
  brain: no other route reaches the index, sandbox, or model.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from acp.common.errors import ACPError
from acp.common.logging import configure_logging, get_logger, log_context
from acp.common.types import new_id
from acp.config import Settings, get_settings
from acp.db.connection import Database, init_db
from acp.gateway.dashboard_router import router as dashboard_router
from acp.gateway.metrics import METRICS
from acp.gateway.tasks_router import router as tasks_router
from acp.sandbox_client import build_sandbox_client
from acp.sandbox_client.interface import SandboxClient

# Static assets directory — served by the gateway, never privileged
_STATIC_DIR = Path(__file__).parent / "static"

_log = get_logger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    sandbox: SandboxClient | None = None,
) -> FastAPI:
    """Build the ASGI app. Dependencies are injectable so tests can pass a
    temp-DB ``Settings`` and a fake sandbox.

    Phase 6 note: ``sandbox`` kwarg is kept for unit tests that inject a
    StubSandboxClient. In production (``make up``), sandbox is None and the
    real Docker runner is built from settings.
    """
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    # /readyz seam: use the REAL sandbox runner unless a test explicitly injects
    # a stub. build_sandbox_client reads settings.sandbox_runner; in docker-less
    # CI tests, the injected StubSandboxClient short-circuits this path.
    readyz_sandbox: SandboxClient = sandbox or build_sandbox_client(settings)

    # Phase 6: initialise the DB and share it on app state (one connection per
    # process — the ASGI server is single-process by default).
    init_db(settings.sqlite_path)
    db = Database(settings.sqlite_path)

    # Wire the orchestrator. The sandbox passed to the orchestrator is the same
    # real-or-stub sandbox used for /readyz, so both surfaces reflect the same runner.
    orchestrator_sandbox = sandbox or build_sandbox_client(settings)

    from acp.orchestrator.service import OrchestratorImpl

    orchestrator = OrchestratorImpl(
        db=db,
        workspace_root=settings.workspace_root,
        sandbox=orchestrator_sandbox,
        default_token_budget=settings.default_task_token_budget,
        default_step_budget=settings.default_task_step_budget,
        default_wall_clock_seconds=settings.default_task_wall_clock_seconds,
    )

    app = FastAPI(title="ACP Gateway", version="0.0.0", docs_url="/docs")
    app.state.settings = settings
    app.state.db = db
    app.state.sandbox = readyz_sandbox
    app.state.orchestrator = orchestrator

    # ── request middleware ────────────────────────────────────────────────────
    @app.middleware("http")
    async def correlate(request: Request, call_next: Any) -> Any:
        request_id = new_id("req")
        with log_context(request_id=request_id, path=request.url.path):
            METRICS.inc("acp_http_requests_total")
            response = await call_next(request)
            response.headers["x-request-id"] = request_id
            return response

    @app.exception_handler(ACPError)
    async def on_acp_error(_: Request, exc: ACPError) -> JSONResponse:
        _log.warning("acp_error", extra={"code": exc.code, "detail": exc.message})
        return JSONResponse(status_code=exc.http_status, content={"error": exc.code})

    # ── operability surface ───────────────────────────────────────────────────
    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        """Liveness: process is up."""
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        """Readiness: DB reachable AND real sandbox healthy.

        Phase 6 repoint: uses the real DockerSandboxRunner (from settings)
        rather than the Phase-0 StubSandboxClient. /readyz is now an honest
        reflection of whether the system can actually serve verified changes.
        """
        checks: dict[str, bool] = {}
        try:
            db.conn.execute("SELECT 1;").fetchone()
            checks["database"] = True
        except Exception as exc:  # noqa: BLE001
            _log.error("readyz.db_unreachable", extra={"detail": str(exc)})
            checks["database"] = False
        try:
            checks["sandbox"] = bool(readyz_sandbox.healthy())
        except Exception as exc:  # noqa: BLE001
            _log.error("readyz.sandbox_unreachable", extra={"detail": str(exc)})
            checks["sandbox"] = False

        ready = all(checks.values())
        return JSONResponse(
            status_code=200 if ready else 503,
            content={"status": "ready" if ready else "not_ready", "checks": checks},
        )

    @app.get("/metrics")
    def metrics() -> PlainTextResponse:
        return PlainTextResponse(METRICS.render())

    # ── static dashboard (Phase 9) — unprivileged consumer ───────────────────
    # GET /dashboard serves the vanilla HTML/CSS/JS bundle.
    # The bundle reads ONLY the /v1/dashboard/* + /v1/tasks/* public endpoints.
    # This route has NO access to the index, sandbox, or model — it is a static
    # file server, not a privileged path.
    @app.get("/dashboard", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(_STATIC_DIR / "dashboard.html", media_type="text/html")

    # ── /v1 consumer surface — the only door into the agent brain ─────────────
    # API-only boundary: this is the complete set of externally reachable routes.
    # No route exposes the raw index, sandbox exec, or model directly.
    app.include_router(tasks_router)
    # Phase 9: dashboard aggregate endpoints — auth-scoped, metering data only.
    app.include_router(dashboard_router)

    _log.info(
        "gateway.created",
        extra={"env": settings.env, "model_backend": settings.model_backend.value},
    )
    return app


# Uvicorn entrypoint: `uvicorn acp.gateway.app:app`
app = create_app()
