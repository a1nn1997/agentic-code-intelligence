"""Module contracts: stubs satisfy their Protocols and fail loudly; and the
API-only boundary holds — no internal module exposes an ASGI app / port.
"""

from __future__ import annotations

import importlib

import pytest

from acp.common.errors import NotImplementedInPhase
from acp.model_gateway import StubModelBackend
from acp.model_gateway.interface import ModelBackend, ModelRequest
from acp.orchestrator import StubOrchestrator
from acp.orchestrator.interface import Orchestrator
from acp.retrieval import StubRetrievalService
from acp.retrieval.interface import RetrievalService
from acp.sandbox_client import StubSandboxClient
from acp.sandbox_client.interface import SandboxClient
from acp.workspace import StubWorkspaceService
from acp.workspace.interface import WorkspaceService

pytestmark = pytest.mark.unit


def test_stubs_satisfy_runtime_checkable_protocols() -> None:
    assert isinstance(StubWorkspaceService(), WorkspaceService)
    assert isinstance(StubRetrievalService(), RetrievalService)
    assert isinstance(StubSandboxClient(), SandboxClient)
    assert isinstance(StubModelBackend(), ModelBackend)
    assert isinstance(StubOrchestrator(), Orchestrator)


def test_stubs_fail_loudly() -> None:
    # Still-deferred seams fail loudly rather than returning a plausible-but-wrong
    # value. (The model-backend stub is no longer here: Phase 4 gave it real
    # deterministic behaviour — see test_model_gateway.py.)
    with pytest.raises(NotImplementedInPhase):
        StubWorkspaceService().create_workspace("u", "repo://x")
    with pytest.raises(NotImplementedInPhase):
        StubOrchestrator().resume("task_x")


def test_stub_model_backend_is_now_deterministic_not_a_stub() -> None:
    # Phase 4: the stub backend returns a real, deterministic XML action.
    resp = StubModelBackend().complete(
        ModelRequest(task_id="t", step_index=0, instruction_xml="<instruction>x</instruction>")
    )
    assert resp.backend == "stub"
    assert "<action" in resp.content_xml


def test_sandbox_stub_is_healthy_but_verify_fails_loudly() -> None:
    # healthy() returns True so /readyz can be 200 in stub mode...
    assert StubSandboxClient().healthy() is True
    # ...but actually verifying still fails loudly (no fake "verified" result).
    with pytest.raises(NotImplementedInPhase):
        from acp.sandbox_client.interface import VerificationRequest

        StubSandboxClient().verify(
            VerificationRequest(
                task_id="t", workspace_id="w", base_commit="c", patch=""
            )
        )


@pytest.mark.parametrize(
    "module_name",
    [
        "acp.retrieval",
        "acp.orchestrator",
        "acp.workspace",
        "acp.sandbox_client",
        "acp.model_gateway",
        "acp.db",
        "acp.config",
        "acp.common",
    ],
)
def test_internal_modules_expose_no_asgi_app(module_name: str) -> None:
    """API-only boundary: only the gateway may define an ASGI ``app``. Any
    internal module sprouting one would be a second listener — forbidden."""
    module = importlib.import_module(module_name)
    assert not hasattr(module, "app"), f"{module_name} must not expose an ASGI app"
