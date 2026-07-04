"""Phase-4 oracle clause (c), proven by the REAL mechanism: verified_success
derives from the actual Docker sandbox verdict.

Gated on Docker + the acp-sandbox image (skipped, not failed, when absent) so
the non-Docker suite stays green while ``make test-integration`` exercises the
genuine oracle. The whole loop runs against the real Phase-3 runner: the agent's
edit is applied inside the container, the repo's build + test suite runs, and
``verified_success`` is reached ONLY because the sandbox returned
applied ∧ built ∧ tests_passed. A model self-report cannot produce it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.docker_gate import requires_sandbox as _requires_sandbox

from acp.common.types import TaskState
from acp.db import Database, JournalRepo, TasksRepo, UsersRepo, init_db
from acp.model_gateway import build_model_gateway
from acp.orchestrator import AgentLoop, LoopConfig
from acp.sandbox_client.docker_runner import DockerSandboxRunner, SandboxLimits
from acp.workspace import WorkspaceServiceImpl

pytestmark = [pytest.mark.integration, pytest.mark.docker]

_IMAGE = "acp-sandbox:latest"


def requires_sandbox(func: object) -> object:
    """Gate this test on the real sandbox image (A8: fail-not-skip when the
    image is buildable but ``ACP_REQUIRE_DOCKER=1``)."""
    return _requires_sandbox(_IMAGE)(func)  # type: ignore[arg-type]


@requires_sandbox
def test_end_to_end_verified_from_real_sandbox(tmp_path: Path, sample_repo: Path) -> None:
    init_db(str(tmp_path / "d.db"))
    db = Database(str(tmp_path / "d.db"))
    UsersRepo(db).create("u")
    root = str(tmp_path / "ws")
    ws = WorkspaceServiceImpl(db, root)
    ref = ws.create_workspace("u", str(sample_repo))
    ws.build_index("u", ref.workspace_id)
    tid = TasksRepo(db).create(
        "u",
        ref.workspace_id,
        "add a passing test target_symbol=serialize_user",
        token_budget=200_000,
        step_budget=40,
        wall_clock_seconds=120,
    ).id

    sandbox = DockerSandboxRunner(image=_IMAGE, limits=SandboxLimits(wall_clock_seconds=120))
    cfg = LoopConfig(db=db, workspace_root=root, gateway=build_model_gateway(), sandbox=sandbox)
    status = AgentLoop(cfg).run(tid)

    assert status.state == TaskState.VERIFIED_SUCCESS
    # The VERIFY journal row must carry the real sandbox verdict.
    verify = [e for e in JournalRepo(db).get_trace(tid) if e.kind == "verify"][-1]
    import json

    payload = json.loads(verify.payload_json)
    assert payload["verified"] is True
    assert payload["built"] is True and payload["tests_passed"] is True
