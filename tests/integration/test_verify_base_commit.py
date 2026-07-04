"""A2 regression: the sandbox verify request carries the resolved base COMMIT.

`_do_verify` built its `VerificationRequest` with `base_commit=task.workspace_id`
— the workspace *id*, not the snapshot commit the worktree/patch derive from. A
runner that trusts `base_commit` (e.g. to check out the right base) would verify
against the wrong tree. This test captures the request the loop sends and asserts
`base_commit` equals the workspace head commit (`self._snap.commit`) and is NOT
the workspace_id.

Before the fix `base_commit == workspace_id` → FAILS. After: it is the commit.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.integration.test_agent_loop import _AGENT_FILE, _INSTRUCTION

from acp.common.types import TaskState
from acp.db import Database, TasksRepo, UsersRepo, init_db
from acp.model_gateway import build_model_gateway
from acp.orchestrator import AgentLoop, LoopConfig
from acp.sandbox_client.interface import (
    VerificationRequest,
    VerificationResult,
    VerificationStage,
)
from acp.workspace import WorkspaceServiceImpl

pytestmark = pytest.mark.integration


class _SpySandbox:
    """Records every VerificationRequest; returns a verdict from the patch (so
    the loop still reaches verified_success without Docker)."""

    def __init__(self) -> None:
        self.requests: list[VerificationRequest] = []

    def verify(self, request: VerificationRequest) -> VerificationResult:  # pragma: no cover
        raise NotImplementedError

    def healthy(self) -> bool:
        return True

    def verify_snapshot(
        self, request: VerificationRequest, snapshot_dir: Path
    ) -> VerificationResult:
        self.requests.append(request)
        import json

        ops = json.loads(request.patch)["ops"]
        ok = any(o["path"] == _AGENT_FILE for o in ops)
        return VerificationResult(
            verified=ok, applied=ok, built=ok, tests_passed=ok,
            exit_code=0 if ok else 1,
            stage=VerificationStage.DONE if ok else VerificationStage.TEST,
        )


def test_verify_request_carries_resolved_commit_not_workspace_id(
    tmp_path: Path, sample_repo: Path
) -> None:
    init_db(str(tmp_path / "d.db"))
    db = Database(str(tmp_path / "d.db"))
    UsersRepo(db).create("u")
    root = str(tmp_path / "ws")
    ws = WorkspaceServiceImpl(db, root)
    ref = ws.create_workspace("u", str(sample_repo))
    ws.build_index("u", ref.workspace_id)
    head = ws.get_workspace("u", ref.workspace_id).head_commit

    tid = TasksRepo(db).create(
        "u", ref.workspace_id, _INSTRUCTION,
        token_budget=200_000, step_budget=40, wall_clock_seconds=900,
    ).id
    spy = _SpySandbox()
    cfg = LoopConfig(db=db, workspace_root=root, gateway=build_model_gateway(), sandbox=spy)
    status = AgentLoop(cfg).run(tid)
    assert status.state == TaskState.VERIFIED_SUCCESS

    assert spy.requests, "the loop must have issued a verify request"
    req = spy.requests[-1]
    assert req.base_commit == head, (
        f"verify base_commit must be the resolved commit {head!r}, got {req.base_commit!r}"
    )
    assert req.base_commit != ref.workspace_id, (
        "verify base_commit must NOT be the workspace_id (the A2 bug)"
    )
