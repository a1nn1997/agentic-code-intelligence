"""Interface contract for workspace management.

Note the shape of every method: it takes a ``user_id`` and returns only data
that belongs to that user. There is deliberately no ``get_any_workspace(id)``
primitive — the interface itself makes cross-user addressing hard to express.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field


class WorkspaceRef(BaseModel):
    """A handle to an ingested workspace, always scoped to its owner."""

    workspace_id: str
    user_id: str
    source: str = Field(description="Git URL or archive name the repo came from")
    head_commit: str | None = Field(
        default=None, description="Current indexed commit of the bare mirror"
    )


class WorktreeHandle(BaseModel):
    """A per-task isolated checkout derived from the workspace's bare mirror.

    Concurrent tasks each get their own worktree so they never clobber one
    another; commit-back is guarded by an advisory lock + base-commit check
    (Phase 4). Phase 0 defines the handle only.
    """

    task_id: str
    workspace_id: str
    path: str
    base_commit: str


@runtime_checkable
class WorkspaceService(Protocol):
    """Ingest repos and vend isolated worktrees, all user-scoped."""

    def create_workspace(self, user_id: str, source: str) -> WorkspaceRef:
        """Ingest ``source`` (Git URL or archive) into a new user-scoped workspace."""
        ...

    def get_workspace(self, user_id: str, workspace_id: str) -> WorkspaceRef:
        """Fetch a workspace the user owns; raises ``NotFound`` / ``IsolationViolation``."""
        ...

    def list_workspaces(self, user_id: str) -> list[WorkspaceRef]:
        """All workspaces owned by ``user_id`` — never any other user's."""
        ...

    def open_worktree(self, user_id: str, workspace_id: str, task_id: str) -> WorktreeHandle:
        """Create an isolated worktree for a task from the workspace's bare mirror."""
        ...
