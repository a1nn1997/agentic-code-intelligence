"""Phase-0 stub. Every method fails loudly via ``NotImplementedInPhase`` so an
accidental early call is unmistakable. Real ingestion + isolation land in
Phase 1/4.
"""

from __future__ import annotations

from acp.common.errors import NotImplementedInPhase
from acp.workspace.interface import WorkspaceRef, WorktreeHandle


class StubWorkspaceService:
    """Satisfies :class:`acp.workspace.interface.WorkspaceService` structurally."""

    def create_workspace(self, user_id: str, source: str) -> WorkspaceRef:
        raise NotImplementedInPhase("workspace.create_workspace lands in Phase 1")

    def get_workspace(self, user_id: str, workspace_id: str) -> WorkspaceRef:
        raise NotImplementedInPhase("workspace.get_workspace lands in Phase 1")

    def list_workspaces(self, user_id: str) -> list[WorkspaceRef]:
        raise NotImplementedInPhase("workspace.list_workspaces lands in Phase 1")

    def open_worktree(self, user_id: str, workspace_id: str, task_id: str) -> WorktreeHandle:
        raise NotImplementedInPhase("workspace.open_worktree lands in Phase 4")
