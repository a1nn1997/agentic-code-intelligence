"""Phase-5 oracle: concurrency conflict handling — serialize or reject, no silent clobber.

Oracle clauses (from phase5_prompt.xml):

(5) CONCURRENCY: two tasks committing to the same workspace do not silently clobber —
    one wins, the other is rejected or rebased, and the final state is consistent.
    Assert no lost write (the winner's change survives; the loser is explicitly rejected).

Mechanism: base-commit check + per-workspace advisory lock (fcntl.flock LOCK_EX).
The commit_worktree method:
  1. Acquires an exclusive advisory lock on a per-workspace .commit.lock file.
  2. Checks current workspace head_commit == the base_commit the worktree was opened from.
  3. If equal: fast-forward copy + advance head_commit.
  4. If different: raise ConflictError — rejected, not silently clobbered.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from acp.common.errors import ConflictError
from acp.db import Database, UsersRepo, init_db
from acp.workspace.service import WorkspaceServiceImpl as WS

pytestmark = pytest.mark.integration


@pytest.fixture
def ws_env(tmp_path: Path, sample_repo: Path) -> tuple[Database, WS, str, str, str]:
    db_path = str(tmp_path / "d.db")
    init_db(db_path)
    db = Database(db_path)
    UsersRepo(db).create("u")
    root = str(tmp_path / "ws")
    ws = WS(db, root)
    ref = ws.create_workspace("u", str(sample_repo))
    return db, ws, "u", ref.workspace_id, db_path


# --- base-commit check: reject if head advanced --------------------------------
def test_commit_worktree_succeeds_when_base_matches(
    ws_env: tuple,
) -> None:
    """First commit: worktree base == workspace head → fast-forward succeeds."""
    db, ws, user, wsid, _db_path = ws_env
    handle = ws.open_worktree(user, wsid, "task-A")
    # Write a file in the worktree.
    (Path(handle.path) / "proof.txt").write_text("from task A")
    new_head = ws.commit_worktree(user, wsid, "task-A", handle.base_commit)
    assert new_head != handle.base_commit, "head should advance after commit"
    ref = ws.get_workspace(user, wsid)
    assert ref.head_commit == new_head


def test_commit_worktree_rejected_if_head_advanced(ws_env: tuple) -> None:
    """Second commit: another task already committed → base-commit mismatch → ConflictError.

    This is the no-silent-clobber guarantee. The second task is EXPLICITLY rejected
    (ConflictError), not silently overwriting the first task's changes.
    """
    db, ws, user, wsid, _db_path = ws_env

    # Task A opens worktree, commits first.
    handle_a = ws.open_worktree(user, wsid, "task-A")
    (Path(handle_a.path) / "from_a.txt").write_text("from task A")
    ws.commit_worktree(user, wsid, "task-A", handle_a.base_commit)

    # Task B opened worktree from the SAME original base (before A committed).
    handle_b = ws.open_worktree(user, wsid, "task-B")
    (Path(handle_b.path) / "from_b.txt").write_text("from task B")

    # B tries to commit with stale base → must be rejected.
    with pytest.raises(ConflictError) as exc_info:
        ws.commit_worktree(user, wsid, "task-B", handle_a.base_commit)

    err_msg = str(exc_info.value).lower()
    assert "head advanced" in err_msg or "conflict" in err_msg, (
        "ConflictError should mention the head mismatch"
    )


def test_no_lost_write_winner_change_survives(ws_env: tuple) -> None:
    """NO LOST WRITE proof: after the winner commits, the loser is rejected and
    the winner's change is intact in the base workspace.

    Final state consistency: the workspace contains exactly what the winner wrote,
    nothing from the loser (which was rejected before it could clobber).
    """
    db, ws, user, wsid, _db_path = ws_env

    handle_a = ws.open_worktree(user, wsid, "task-A")
    # Winner writes a distinguishable marker.
    (Path(handle_a.path) / "winner.txt").write_text("WINNER_CONTENT_FROM_TASK_A")
    ws.commit_worktree(user, wsid, "task-A", handle_a.base_commit)

    handle_b = ws.open_worktree(user, wsid, "task-B")
    (Path(handle_b.path) / "loser.txt").write_text("LOSER_CONTENT_FROM_TASK_B")

    # Loser is rejected.
    with pytest.raises(ConflictError):
        ws.commit_worktree(user, wsid, "task-B", handle_a.base_commit)

    # Winner's content is present in the workspace base.
    base_path = ws.repo_path(user, wsid)
    winner_file = base_path / "winner.txt"
    assert winner_file.exists(), "winner's file must be in the base workspace"
    assert winner_file.read_text() == "WINNER_CONTENT_FROM_TASK_A", (
        "winner's content must be intact — not overwritten by the loser"
    )

    # Loser's content is NOT in the base (it was rejected).
    loser_file = base_path / "loser.txt"
    assert not loser_file.exists(), (
        "loser's file must NOT be in the base workspace — the commit was rejected"
    )


# --- advisory lock: concurrent commits are serialized, not racing ---------------
def test_concurrent_commits_serialized_not_racing(ws_env: tuple) -> None:
    """Two threads committing concurrently: the advisory lock serializes them.

    One thread wins (commits fast-forward), the other is rejected with ConflictError
    (base changed while it was waiting for the lock). No data race, no silent clobber.

    This proves: serialize OR reject, never silent clobber.

    Each thread gets its own DB connection (SQLite WAL allows concurrent readers;
    BEGIN IMMEDIATE serializes writers; the advisory flock on the .commit.lock file
    is the additional workspace-level serialization mechanism we are testing here).
    """
    db, ws, user, wsid, db_path = ws_env
    root = ws._root  # noqa: SLF001 — test needs the path

    # Both tasks open worktrees from the same base before either commits.
    handle_a = ws.open_worktree(user, wsid, "task-concurrent-A")
    handle_b = ws.open_worktree(user, wsid, "task-concurrent-B")
    assert handle_a.base_commit == handle_b.base_commit, (
        "both tasks must open from the same base for this test"
    )

    (Path(handle_a.path) / "from_a.txt").write_text("A_CONTENT")
    (Path(handle_b.path) / "from_b.txt").write_text("B_CONTENT")

    results: list[str | Exception] = []

    def commit_a() -> None:
        # Each thread uses its own DB connection to avoid SQLite thread-safety issues.
        thread_db = Database(db_path)
        thread_ws = WS(thread_db, root)
        try:
            new_head = thread_ws.commit_worktree(
                user, wsid, "task-concurrent-A", handle_a.base_commit
            )
            results.append(f"OK:{new_head[:12]}")
        except Exception as e:
            results.append(e)

    def commit_b() -> None:
        thread_db = Database(db_path)
        thread_ws = WS(thread_db, root)
        try:
            new_head = thread_ws.commit_worktree(
                user, wsid, "task-concurrent-B", handle_b.base_commit
            )
            results.append(f"OK:{new_head[:12]}")
        except Exception as e:
            results.append(e)

    t1 = threading.Thread(target=commit_a)
    t2 = threading.Thread(target=commit_b)
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    # Exactly one success, one ConflictError — never two successes (which would be a race).
    successes = [r for r in results if isinstance(r, str) and r.startswith("OK:")]
    conflicts = [r for r in results if isinstance(r, ConflictError)]
    errors = [r for r in results if isinstance(r, Exception) and not isinstance(r, ConflictError)]

    assert not errors, f"unexpected errors: {errors}"
    assert len(successes) == 1, (
        f"expected exactly 1 successful commit, got {len(successes)}. "
        f"Results: {results}. "
        "Two successes = silent clobber (data race)."
    )
    assert len(conflicts) == 1, (
        f"expected exactly 1 ConflictError, got {len(conflicts)}. "
        f"Results: {results}."
    )

    # Final state: workspace contains the winner's content.
    base_path = ws.repo_path(user, wsid)
    # One of the two files must be present (the winner's).
    a_exists = (base_path / "from_a.txt").exists()
    b_exists = (base_path / "from_b.txt").exists()
    # The winner committed; at least one file must be there.
    assert a_exists or b_exists, "winner's file must survive in the base"
    # And both cannot be there unless the loser somehow also committed (which is the bug).
    # If A won, only a_exists. If B won, only b_exists.
    # (Both being present would mean both committed — the silent-clobber scenario.)
    # We don't assert which task won, only that exactly one did.
    assert not (a_exists and b_exists), (
        "both files present = both tasks committed = silent clobber (data race). "
        "The advisory lock should have prevented this."
    )
