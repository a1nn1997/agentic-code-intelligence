"""A7 oracle: the wall-clock budget is enforced (not merely stored).

Hard Req #6 names a "wall-clock deadline"; Adversarial #7 is budget exhausted
mid-task. Before this fix ``_budget_breach`` checked only step + token budgets,
so a task on a LIVE model backend could run unbounded in wall-clock. This test
gives a task a tiny wall-clock budget and a slow model step, then asserts:

  * the terminal state is a CLEAN ``BUDGET_EXHAUSTED`` (not a crash),
  * the reason names the wall-clock budget,
  * PARTIAL progress was journaled (at least the first step ran), and
  * the worktree is intact (present, non-empty) — we stopped at a checkpoint,
    not mid-effect.

Before the fix the loop runs to VERIFIED_SUCCESS regardless of elapsed time, so
this test FAILS; after the fix it stops cleanly on the deadline.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from tests.integration.test_agent_loop import _INSTRUCTION, FakeSandbox

from acp.common.types import TaskState
from acp.db import Database, JournalRepo, TasksRepo, UsersRepo, init_db
from acp.model_gateway import build_model_gateway
from acp.model_gateway.interface import ModelRequest, ModelResponse
from acp.orchestrator import AgentLoop, LoopConfig
from acp.workspace import WorkspaceServiceImpl

pytestmark = pytest.mark.integration


class _SlowGateway:
    """Wraps the real stub gateway and makes each model call take ~0.6s.

    A real slow step (not a mocked clock) so the deadline is exercised through
    the same monotonic() path production uses. Two steps of ~0.6s exceed a 1s
    wall-clock budget, while step 0 still completes and journals first.
    """

    def __init__(self, delay: float = 0.6) -> None:
        self._inner = build_model_gateway()
        self._delay = delay

    def complete(self, request: ModelRequest) -> ModelResponse:
        time.sleep(self._delay)
        return self._inner.complete(request)


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


def test_wall_clock_budget_stops_clean_with_partial_progress(env: tuple) -> None:
    db, root, user, ws = env
    sb = FakeSandbox()
    # 1s wall-clock budget; generous step/token budgets so ONLY wall-clock trips.
    tid = TasksRepo(db).create(
        user, ws, _INSTRUCTION, token_budget=200_000, step_budget=40, wall_clock_seconds=1
    ).id
    cfg = LoopConfig(db=db, workspace_root=root, gateway=_SlowGateway(), sandbox=sb)

    status = AgentLoop(cfg).run(tid)

    # Clean terminal on the wall-clock deadline.
    assert status.state == TaskState.BUDGET_EXHAUSTED, (
        f"expected BUDGET_EXHAUSTED on wall-clock, got {status.state}"
    )
    assert status.reason and "wall-clock" in status.reason, status.reason

    # Partial progress: at least the first step journaled before the deadline.
    steps = [e.step_index for e in JournalRepo(db).get_trace(tid)]
    assert steps, "expected at least one journaled step (partial progress)"
    assert min(steps) == 0

    # Worktree intact: present and non-empty (we stopped at a checkpoint, not
    # mid-effect leaving a corrupt tree).
    ws_svc = WorkspaceServiceImpl(db, root)
    handle = ws_svc.open_worktree(user, ws, tid)
    wt = Path(handle.path)
    assert wt.is_dir() and any(wt.iterdir()), "worktree must be intact after clean stop"
