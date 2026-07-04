"""Phase-4 oracle (partial): per-task worktree isolation by construction.

Two concurrent tasks must never clobber each other or the base repo. We prove
it by the actual mechanism — disjoint materialized directories — not a proxy:
editing inside one worktree leaves the base tree and a peer worktree
byte-identical, and re-opening a worktree is idempotent (resume-safe).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from acp.db import Database, UsersRepo, init_db
from acp.workspace import WorkspaceServiceImpl

pytestmark = pytest.mark.integration

_USER = "user_wt"


def _service(tmp_path: Path) -> WorkspaceServiceImpl:
    db_path = tmp_path / "wt.db"
    init_db(str(db_path))
    db = Database(str(db_path))
    UsersRepo(db).create(_USER)
    return WorkspaceServiceImpl(db, tmp_path / "workspaces")


def test_two_tasks_get_disjoint_worktrees(tmp_path: Path, sample_repo: Path) -> None:
    svc = _service(tmp_path)
    ref = svc.create_workspace(_USER, str(sample_repo))
    a = svc.open_worktree(_USER, ref.workspace_id, "task_a")
    b = svc.open_worktree(_USER, ref.workspace_id, "task_b")
    assert a.path != b.path
    assert Path(a.path).is_dir() and Path(b.path).is_dir()
    # base_commit is recorded so a commit-back can base-check against it.
    assert a.base_commit == ref.head_commit


def test_edit_in_worktree_does_not_touch_base_or_peer(
    tmp_path: Path, sample_repo: Path
) -> None:
    svc = _service(tmp_path)
    ref = svc.create_workspace(_USER, str(sample_repo))
    a = svc.open_worktree(_USER, ref.workspace_id, "task_a")
    b = svc.open_worktree(_USER, ref.workspace_id, "task_b")
    base_file = svc.repo_path(_USER, ref.workspace_id) / "backend/app/users/service.py"
    peer_file = Path(b.path) / "backend/app/users/service.py"
    base_before = base_file.read_text()
    peer_before = peer_file.read_text()

    # Mutate a file *inside* worktree A only.
    (Path(a.path) / "backend/app/users/service.py").write_text("# clobbered in A\n")

    assert base_file.read_text() == base_before, "base repo was mutated"
    assert peer_file.read_text() == peer_before, "peer worktree was mutated"


def test_reopen_is_idempotent_and_preserves_edits(
    tmp_path: Path, sample_repo: Path
) -> None:
    svc = _service(tmp_path)
    ref = svc.create_workspace(_USER, str(sample_repo))
    first = svc.open_worktree(_USER, ref.workspace_id, "task_a")
    marker = Path(first.path) / "AGENT_MARKER.txt"
    marker.write_text("applied once\n")
    # Re-open (as a resume would): same dir, edits intact — not re-snapshotted.
    second = svc.open_worktree(_USER, ref.workspace_id, "task_a")
    assert second.path == first.path
    assert marker.read_text() == "applied once\n"
