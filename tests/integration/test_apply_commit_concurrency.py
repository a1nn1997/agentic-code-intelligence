"""A9 oracle: the concurrent-write guarantee is wired into the PRODUCTION apply path.

Adversarial #6 — "two tasks edit the same workspace concurrently → never silent
clobber" — was previously proven only in a helper (`commit_worktree`) that the
agent loop never called: the loop applied edits to its worktree and terminated
WITHOUT committing back through the guarded path. This test drives TWO real
`AgentLoop.run()` calls (APPLY mode) on ONE workspace concurrently and asserts:

  * exactly ONE task reaches VERIFIED_SUCCESS (its commit fast-forwarded), and
  * exactly ONE task is rejected via the guarded path (base-commit conflict →
    the loop terminates GAVE_UP with a "commit conflict" reason),

so the guarantee is enforced by the production flow, not just a unit test.

Before the fix both tasks reach VERIFIED_SUCCESS (neither commits, so neither
conflicts) — the test FAILS. After the fix exactly one wins and one is rejected.
"""

from __future__ import annotations

import threading
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
def env(tmp_path: Path, sample_repo: Path) -> tuple[str, str, str, str]:
    """A migrated DB + one ingested/indexed workspace shared by both tasks."""
    db_path = str(tmp_path / "d.db")
    root = str(tmp_path / "ws")
    init_db(db_path)
    db = Database(db_path)
    UsersRepo(db).create("u")
    ws = WorkspaceServiceImpl(db, root)
    ref = ws.create_workspace("u", str(sample_repo))
    ws.build_index("u", ref.workspace_id)
    db.close()
    return db_path, root, "u", ref.workspace_id


def test_two_concurrent_apply_tasks_one_commit_one_conflict(env: tuple) -> None:
    db_path, root, user, ws_id = env

    # Create both APPLY tasks up front (default mode is APPLY).
    setup_db = Database(db_path)
    tid_a = TasksRepo(setup_db).create(
        user, ws_id, _INSTRUCTION, token_budget=200_000, step_budget=40, wall_clock_seconds=900
    ).id
    tid_b = TasksRepo(setup_db).create(
        user, ws_id, _INSTRUCTION, token_budget=200_000, step_budget=40, wall_clock_seconds=900
    ).id
    setup_db.close()

    results: dict[str, TaskState] = {}
    reasons: dict[str, str | None] = {}
    barrier = threading.Barrier(2)

    def run_task(tid: str) -> None:
        # Each thread gets its own Database + loop, like two concurrent workers.
        db = Database(db_path)
        cfg = LoopConfig(
            db=db, workspace_root=root, gateway=build_model_gateway(), sandbox=FakeSandbox()
        )
        loop = AgentLoop(cfg)
        barrier.wait()  # both open their worktree from the same base, then race to commit
        status = loop.run(tid)
        results[tid] = status.state
        reasons[tid] = status.reason
        db.close()

    ta = threading.Thread(target=run_task, args=(tid_a,))
    tb = threading.Thread(target=run_task, args=(tid_b,))
    ta.start()
    tb.start()
    ta.join(timeout=30)
    tb.join(timeout=30)

    states = list(results.values())
    successes = [s for s in states if s == TaskState.VERIFIED_SUCCESS]
    gaveups = [tid for tid, s in results.items() if s == TaskState.GAVE_UP]

    assert len(states) == 2, f"both tasks must finish; got {results}"
    assert len(successes) == 1, (
        f"expected exactly ONE VERIFIED_SUCCESS (one commit), got {len(successes)}: "
        f"{results}. Two successes = silent clobber (the guarantee is not wired in)."
    )
    assert len(gaveups) == 1, f"expected exactly ONE rejected task, got: {results}"

    # The loser was rejected specifically via the guarded commit path.
    loser = gaveups[0]
    assert reasons[loser] and "commit conflict" in reasons[loser], (
        f"loser must be rejected via the base-commit check, got reason={reasons[loser]!r}"
    )
