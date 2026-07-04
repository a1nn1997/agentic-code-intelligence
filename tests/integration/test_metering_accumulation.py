"""A3 oracle: per-task metering for retrieval bytes + sandbox seconds is WRITTEN.

`tasks.retrieval_bytes` and `tasks.sandbox_seconds` columns existed and were
surfaced in `TaskStatus`, but the loop never accumulated into them — both stayed
zero for every run. This test drives a full happy-path task (plan → retrieve →
edit → verify) and asserts BOTH counters end non-zero, so the $/task and
capacity story rests on real numbers rather than zeros.

Before the fix both are 0 → the test FAILS. After the fix `_do_retrieve` adds
`RetrievalResult.byte_count` and `_do_verify` adds the sandbox duration.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.integration.test_agent_loop import _INSTRUCTION, FakeSandbox

from acp.common.types import TaskState
from acp.db import Database, TasksRepo, UsersRepo, init_db
from acp.model_gateway import build_model_gateway
from acp.orchestrator import AgentLoop, LoopConfig
from acp.workspace import WorkspaceServiceImpl

pytestmark = pytest.mark.integration


@pytest.fixture
def env(tmp_path: Path, sample_repo: Path) -> tuple[Database, str, str, str]:
    init_db(str(tmp_path / "d.db"))
    db = Database(str(tmp_path / "d.db"))
    UsersRepo(db).create("u")
    root = str(tmp_path / "ws")
    ws = WorkspaceServiceImpl(db, root)
    ref = ws.create_workspace("u", str(sample_repo))
    ws.build_index("u", ref.workspace_id)
    return db, root, "u", ref.workspace_id


def test_retrieval_bytes_and_sandbox_seconds_nonzero_after_run(env: tuple) -> None:
    db, root, user, ws = env
    tid = TasksRepo(db).create(
        user, ws, _INSTRUCTION, token_budget=200_000, step_budget=40, wall_clock_seconds=900
    ).id
    cfg = LoopConfig(
        db=db, workspace_root=root, gateway=build_model_gateway(), sandbox=FakeSandbox()
    )
    status = AgentLoop(cfg).run(tid)
    assert status.state == TaskState.VERIFIED_SUCCESS

    task = TasksRepo(db).get(user, tid)
    assert task is not None
    assert task.retrieval_bytes > 0, (
        f"retrieval_bytes must accumulate the retrieved span bytes, got "
        f"{task.retrieval_bytes}"
    )
    assert task.sandbox_seconds > 0.0, (
        f"sandbox_seconds must record the verify duration, got {task.sandbox_seconds}"
    )

    # And the metering is surfaced on TaskStatus (the consumer-facing shape).
    assert status.retrieval_bytes == task.retrieval_bytes
    assert status.sandbox_seconds == task.sandbox_seconds
