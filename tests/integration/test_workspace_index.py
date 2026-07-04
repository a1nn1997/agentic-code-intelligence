"""Integration: ingest the sample repo into a per-user workspace, build its
structural index through the service, prove cross-user isolation, and exercise
the incremental path through the persisted index. Exercises the module boundary
(WorkspaceService → index) with no model/network calls."""

from __future__ import annotations

from pathlib import Path

import pytest

from acp.common.errors import IsolationViolation, NotFound
from acp.db import Database, UsersRepo
from acp.workspace import WorkspaceService, WorkspaceServiceImpl

pytestmark = pytest.mark.integration


def _svc(db: Database, root: Path) -> WorkspaceServiceImpl:
    UsersRepo(db).create("user_a")
    UsersRepo(db).create("user_b")
    return WorkspaceServiceImpl(db, root)


def test_service_satisfies_protocol(db: Database, workspace_root: Path) -> None:
    svc = WorkspaceServiceImpl(db, workspace_root)
    assert isinstance(svc, WorkspaceService)


def test_ingest_and_build_index(
    db: Database, workspace_root: Path, sample_repo: Path
) -> None:
    svc = _svc(db, workspace_root)
    ref = svc.create_workspace("user_a", str(sample_repo))
    assert ref.head_commit  # snapshot id recorded

    idx = svc.build_index("user_a", ref.workspace_id)
    # The end-to-end index resolves the cross-file symbol across files.
    assert idx.reference_files("serialize_user", "python") == [
        "backend/app/reports/export.py",
        "backend/app/users/api.py",
        "backend/app/users/service.py",
        "backend/tests/test_users.py",
    ]
    assert ("frontend/src/components/userList.ts", "frontend/src/models/user.ts") in set(
        idx.resolved_import_edges()
    )


def test_cross_user_isolation(
    db: Database, workspace_root: Path, sample_repo: Path
) -> None:
    svc = _svc(db, workspace_root)
    ref = svc.create_workspace("user_a", str(sample_repo))

    # user_b cannot get, index, reach the repo path of, or edit user_a's workspace.
    with pytest.raises(NotFound):
        svc.get_workspace("user_b", ref.workspace_id)
    with pytest.raises(NotFound):
        svc.build_index("user_b", ref.workspace_id)
    with pytest.raises(NotFound):
        svc.repo_path("user_b", ref.workspace_id)
    with pytest.raises(NotFound):
        svc.update_file("user_b", ref.workspace_id, "x.py", "y = 1")

    # user_b's own listing never contains user_a's workspace.
    assert svc.list_workspaces("user_b") == []
    assert [w.workspace_id for w in svc.list_workspaces("user_a")] == [ref.workspace_id]


def test_incremental_update_through_service_matches_full_build(
    db: Database, workspace_root: Path, sample_repo: Path
) -> None:
    svc = _svc(db, workspace_root)
    ref = svc.create_workspace("user_a", str(sample_repo))
    svc.build_index("user_a", ref.workspace_id)

    idx = svc.update_file(
        "user_a",
        ref.workspace_id,
        "backend/app/users/service.py",
        (sample_repo / "backend/app/users/service.py").read_text(encoding="utf-8")
        + "\n\ndef added_via_service() -> int:\n    return 7\n",
    )
    # The incrementally-updated, persisted index reflects the edit...
    assert [s.file_path for s in idx.definitions("added_via_service")] == [
        "backend/app/users/service.py"
    ]
    # ...and equals a full rebuild of the workspace's now-edited repo.
    full = svc.build_index("user_a", ref.workspace_id)
    assert idx.serialize() == full.serialize()


def test_head_changes_when_a_file_is_edited(
    db: Database, workspace_root: Path, sample_repo: Path
) -> None:
    svc = _svc(db, workspace_root)
    ref = svc.create_workspace("user_a", str(sample_repo))
    before = svc.get_workspace("user_a", ref.workspace_id).head_commit
    svc.update_file("user_a", ref.workspace_id, "backend/app/users/service.py",
                    "x = 1\n")
    after = svc.get_workspace("user_a", ref.workspace_id).head_commit
    assert before != after


def test_update_file_rejects_path_traversal(
    db: Database, workspace_root: Path, sample_repo: Path
) -> None:
    """Even the owner cannot write outside their own repo via a crafted path."""
    svc = _svc(db, workspace_root)
    ref = svc.create_workspace("user_a", str(sample_repo))
    with pytest.raises(IsolationViolation):
        svc.update_file("user_a", ref.workspace_id, "../../../../tmp/evil.py", "x = 1")


def test_open_worktree_vends_isolated_checkout(
    db: Database, workspace_root: Path, sample_repo: Path
) -> None:
    """Phase 4: open_worktree now materializes a per-task isolated checkout.

    (Deep isolation properties are covered in test_worktree_isolation.py; here
    we only assert the seam is implemented and scoped to the owning user.)"""
    svc = _svc(db, workspace_root)
    ref = svc.create_workspace("user_a", str(sample_repo))
    handle = svc.open_worktree("user_a", ref.workspace_id, "task_x")
    assert Path(handle.path).is_dir()
    assert handle.base_commit == ref.head_commit
    # A non-owner cannot open a worktree on someone else's workspace.
    with pytest.raises(NotFound):
        svc.open_worktree("user_b", ref.workspace_id, "task_y")
