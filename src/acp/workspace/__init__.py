"""Per-user workspace management: ingest a repository into an isolated,
user-scoped workspace and hand out per-task isolated worktrees.

This module is where user isolation is enforced *by construction* (Phase 1/6):
every accessor is keyed on ``(user_id, workspace_id)`` and cannot address
another user's data. Phase 0 defines the interface only.
"""

from acp.workspace.interface import WorkspaceRef, WorkspaceService, WorktreeHandle
from acp.workspace.service import WorkspaceServiceImpl
from acp.workspace.stub import StubWorkspaceService

__all__ = [
    "WorkspaceService",
    "WorkspaceServiceImpl",
    "StubWorkspaceService",
    "WorkspaceRef",
    "WorktreeHandle",
]
