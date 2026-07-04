"""WorkspaceService — Phase-1 implementation.

Ingests a repo (local directory or archive) into a **per-user** workspace on
disk and owns its structural index. User isolation is *by construction*: every
on-disk path is derived from ``user_id`` **and** ``workspace_id`` together, and
every operation first calls :meth:`get_workspace`, whose backing SQL filters on
``user_id``. There is no primitive that takes a bare ``workspace_id``, so one
user simply cannot express a path into another user's data. A path-containment
check is the belt to that suspenders.

The index is persisted as canonical JSON next to the repo, so incremental
re-index can reload the prior partitions and replace exactly one.
"""

from __future__ import annotations

import fcntl
import hashlib
import shutil
import tarfile
import time
import zipfile
from pathlib import Path

from acp.common.errors import ACPError, ConflictError, IsolationViolation, NotFound
from acp.common.logging import get_logger
from acp.db import Database, WorkspacesRepo
from acp.index import IndexBuilder
from acp.index.model import Index
from acp.workspace.interface import WorkspaceRef, WorktreeHandle

_log = get_logger(__name__)
_INDEX_FILENAME = "index.json"
_REPO_DIRNAME = "repo"
_WORKTREES_DIRNAME = "worktrees"
_IGNORE_ON_COPY = shutil.ignore_patterns(
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"
)


class WorkspaceServiceImpl:
    """Concrete :class:`acp.workspace.interface.WorkspaceService`."""

    def __init__(self, db: Database, workspace_root: str | Path) -> None:
        self._repo = WorkspacesRepo(db)
        self._root = Path(workspace_root).resolve()
        self._builder = IndexBuilder()

    # --- ingestion -----------------------------------------------------------
    def create_workspace(self, user_id: str, source: str) -> WorkspaceRef:
        """Ingest ``source`` (a local dir or .zip/.tar.gz archive) into a new
        user-scoped workspace, then record it + its snapshot id."""
        ws = self._repo.create(user_id, source)
        repo_path = self._repo_path(user_id, ws.id)
        repo_path.mkdir(parents=True, exist_ok=True)
        self._ingest(source, repo_path)
        head = self._snapshot_id(repo_path)
        self._repo.set_head(user_id, ws.id, head)
        _log.info("workspace.created", extra={"workspace_id": ws.id, "user_id": user_id})
        return WorkspaceRef(
            workspace_id=ws.id, user_id=user_id, source=source, head_commit=head
        )

    def get_workspace(self, user_id: str, workspace_id: str) -> WorkspaceRef:
        row = self._repo.get(user_id, workspace_id)
        if row is None:
            # Not owned or nonexistent — indistinguishable to the caller by
            # design, so probing cannot confirm another user's workspace exists.
            raise NotFound(f"workspace {workspace_id} not found for this user")
        return WorkspaceRef(
            workspace_id=row.id,
            user_id=row.user_id,
            source=row.source,
            head_commit=row.head_commit,
        )

    def list_workspaces(self, user_id: str) -> list[WorkspaceRef]:
        return [
            WorkspaceRef(
                workspace_id=r.id,
                user_id=r.user_id,
                source=r.source,
                head_commit=r.head_commit,
            )
            for r in self._repo.list(user_id)
        ]

    def open_worktree(self, user_id: str, workspace_id: str, task_id: str) -> WorktreeHandle:
        """Vend a per-task isolated checkout of the workspace repo.

        Isolation mechanism (Phase 4): a copy-on-first-open snapshot of the repo
        into a per-``task_id`` directory under the workspace. Because the path is
        derived from ``user_id`` + ``workspace_id`` + ``task_id``, two concurrent
        tasks — and the base repo — occupy disjoint directories, so a run's edits
        land only in its own worktree and can never clobber the base or a peer
        (adversarial scenario 6). The ``base_commit`` recorded here is the
        workspace head at open time; committing back is guarded by a
        base-commit check against this value (see :meth:`commit_worktree`).

        Real git worktrees from a bare mirror are the production form; the
        Phase-1 content-hash snapshot id stands in for the commit, so here a
        worktree is a materialized copy. It is created once and reused across
        resumes of the same task (idempotent): a resumed run re-opens the SAME
        directory and finds any already-applied edits intact.
        """
        ref = self.get_workspace(user_id, workspace_id)  # ownership gate
        base = self._repo_path(user_id, workspace_id)
        wt = self._worktree_path(user_id, workspace_id, task_id)
        if not wt.exists():
            # First open: snapshot the base into the isolated worktree dir. On a
            # resume this branch is skipped, so applied edits are preserved.
            wt.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(base, wt, ignore=_IGNORE_ON_COPY)
        return WorktreeHandle(
            task_id=task_id,
            workspace_id=workspace_id,
            path=str(wt),
            base_commit=ref.head_commit or self._snapshot_id(base),
        )

    def commit_worktree(
        self,
        user_id: str,
        workspace_id: str,
        task_id: str,
        base_commit: str,
        timeout_seconds: float = 10.0,
    ) -> str:
        """Commit an isolated worktree back to the workspace base under an advisory lock.

        **Phase-5 concurrency guarantee: serialize or reject, never silent clobber.**

        Mechanism:
        1. Acquire a per-workspace advisory lock (fcntl.flock LOCK_EX) so only one
           writer at a time can check and commit. This is the serialization point.
        2. Check the current workspace head_commit against ``base_commit``:
           - If equal: the worktree was opened from the current head — fast-forward
             merge by copying worktree files to the base and advancing head_commit.
           - If different: another task committed while we were working. Raise
             ``ConflictError`` — the caller must rebase-and-reverify or give up.
           This is the base-commit check: no silent clobber, ever.

        Returns the new head_commit (the new workspace snapshot id) on success.
        Raises ``ConflictError`` if the base has advanced since the worktree was opened.

        Production note: in a real Git-backed workspace this is ``git merge --ff-only``
        with an advisory lock on the bare repo's ``packed-refs``. The content-hash
        snapshot id here is the Phase-1 stand-in for a git commit sha.
        """
        self.get_workspace(user_id, workspace_id)  # ownership gate
        lock_path = self._lock_path(user_id, workspace_id)
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        deadline = time.monotonic() + timeout_seconds
        with open(lock_path, "w") as lock_fh:
            # Try to acquire the exclusive lock (non-blocking + retry with timeout).
            while True:
                try:
                    fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ConflictError(
                            f"workspace {workspace_id} lock timed out after "
                            f"{timeout_seconds}s — another task is committing"
                        ) from None
                    time.sleep(0.05)

            try:
                # Base-commit check: is the workspace still at the base we opened from?
                ref = self._repo.get(user_id, workspace_id)
                if ref is None:
                    raise NotFound(f"workspace {workspace_id} disappeared")
                current_head = ref.head_commit or ""
                if current_head != base_commit:
                    raise ConflictError(
                        f"workspace {workspace_id} head advanced to {current_head!r} "
                        f"since this worktree was opened from {base_commit!r}. "
                        "Rebase-and-reverify or give up."
                    )

                # Fast-forward: copy worktree tree into the base repo.
                wt = self._worktree_path(user_id, workspace_id, task_id)
                if not wt.is_dir():
                    raise NotFound(f"worktree for task {task_id} not found")
                base = self._repo_path(user_id, workspace_id)
                shutil.copytree(wt, base, dirs_exist_ok=True, ignore=_IGNORE_ON_COPY)

                # Advance the head_commit to the new snapshot.
                new_head = self._snapshot_id(base)
                self._repo.set_head(user_id, workspace_id, new_head)
                _log.info(
                    "workspace.committed",
                    extra={
                        "workspace_id": workspace_id,
                        "task_id": task_id,
                        "old_head": base_commit,
                        "new_head": new_head,
                    },
                )
                return new_head
            finally:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)

    def worktree_path(self, user_id: str, workspace_id: str, task_id: str) -> Path:
        """Ownership-verified path to a task's isolated worktree (must exist)."""
        self.get_workspace(user_id, workspace_id)
        wt = self._worktree_path(user_id, workspace_id, task_id)
        if not wt.is_dir():
            raise NotFound(f"no worktree for task {task_id}")
        return wt

    # --- index (internal contract; consumers reach it only via Phase-2) ------
    def build_index(self, user_id: str, workspace_id: str) -> Index:
        """Full build of the workspace index; persists canonical JSON."""
        repo_path = self.repo_path(user_id, workspace_id)  # verifies ownership
        index = self._builder.build(repo_path)
        self._index_path(user_id, workspace_id).write_text(
            index.serialize(), encoding="utf-8"
        )
        return index

    def load_index(self, user_id: str, workspace_id: str) -> Index:
        """Load the persisted index, building it once if absent."""
        self.get_workspace(user_id, workspace_id)  # ownership gate
        path = self._index_path(user_id, workspace_id)
        if not path.exists():
            return self.build_index(user_id, workspace_id)
        return Index.from_serialized(path.read_text(encoding="utf-8"))

    def update_file(
        self, user_id: str, workspace_id: str, rel_path: str, new_content: str
    ) -> Index:
        """Write ``rel_path`` and incrementally re-index only that file."""
        repo_path = self.repo_path(user_id, workspace_id)  # verifies ownership
        target = self._safe_join(repo_path, rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new_content, encoding="utf-8")
        index = self.load_index(user_id, workspace_id)
        self._builder.reindex_file(index, repo_path, rel_path)
        head = self._snapshot_id(repo_path)
        self._repo.set_head(user_id, workspace_id, head)
        self._index_path(user_id, workspace_id).write_text(
            index.serialize(), encoding="utf-8"
        )
        return index

    def repo_path(self, user_id: str, workspace_id: str) -> Path:
        """Ownership-verified path to the workspace's repo (operator/internal)."""
        self.get_workspace(user_id, workspace_id)
        return self._repo_path(user_id, workspace_id)

    # --- path derivation + safety -------------------------------------------
    def _repo_path(self, user_id: str, workspace_id: str) -> Path:
        return self._user_root(user_id) / workspace_id / _REPO_DIRNAME

    def _worktree_path(self, user_id: str, workspace_id: str, task_id: str) -> Path:
        """Per-task isolated checkout dir. Derived from user+workspace+task, so
        distinct tasks are physically disjoint (isolation by construction)."""
        return self._user_root(user_id) / workspace_id / _WORKTREES_DIRNAME / task_id

    def _index_path(self, user_id: str, workspace_id: str) -> Path:
        return self._user_root(user_id) / workspace_id / _INDEX_FILENAME

    def _lock_path(self, user_id: str, workspace_id: str) -> Path:
        """Per-workspace advisory lock file for commit serialization."""
        return self._user_root(user_id) / workspace_id / ".commit.lock"

    def _user_root(self, user_id: str) -> Path:
        root = (self._root / user_id).resolve()
        if self._root not in root.parents and root != self._root:
            raise IsolationViolation("resolved user root escapes the workspace root")
        return root

    def _safe_join(self, base: Path, rel_path: str) -> Path:
        target = (base / rel_path).resolve()
        if base.resolve() not in target.parents and target != base.resolve():
            raise IsolationViolation(f"path escapes workspace: {rel_path}")
        return target

    # --- helpers -------------------------------------------------------------
    def _ingest(self, source: str, dest: Path) -> None:
        src = Path(source)
        if src.is_dir():
            shutil.copytree(src, dest, dirs_exist_ok=True, ignore=_IGNORE_ON_COPY)
            return
        if src.is_file() and zipfile.is_zipfile(src):
            self._extract_zip(src, dest)
            return
        if src.is_file() and tarfile.is_tarfile(src):
            with tarfile.open(src) as tf:
                tf.extractall(dest, filter="data")  # 'data' filter blocks path escape
            return
        raise ACPError(f"unsupported ingest source (expected dir/.zip/.tar): {source}")

    def _extract_zip(self, src: Path, dest: Path) -> None:
        dest_resolved = dest.resolve()
        with zipfile.ZipFile(src) as zf:
            for member in zf.namelist():
                out = (dest / member).resolve()
                if dest_resolved not in out.parents and out != dest_resolved:
                    raise IsolationViolation(f"zip-slip blocked: {member}")
            zf.extractall(dest)

    def _snapshot_id(self, repo_path: Path) -> str:
        """A deterministic content id for the whole tree — the Phase-1 stand-in
        for a git commit (real commits arrive with the bare mirror in Phase 4)."""
        h = hashlib.sha256()
        for f in sorted(p for p in repo_path.rglob("*") if p.is_file()):
            h.update(f.relative_to(repo_path).as_posix().encode("utf-8"))
            h.update(b"\0")
            h.update(hashlib.sha256(f.read_bytes()).digest())
        return h.hexdigest()
