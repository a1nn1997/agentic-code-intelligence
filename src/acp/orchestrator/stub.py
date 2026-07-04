"""Phase-0 stub orchestrator. The hand-written state machine, journal, and
resume/replay land in Phase 4."""

from __future__ import annotations

from collections.abc import Iterator

from acp.common.errors import NotImplementedInPhase
from acp.orchestrator.interface import TaskEvent, TaskRequest, TaskStatus


class StubOrchestrator:
    """Satisfies :class:`acp.orchestrator.interface.Orchestrator` structurally."""

    def submit(self, request: TaskRequest) -> TaskStatus:
        raise NotImplementedInPhase("agent loop submit lands in Phase 4")

    def get_status(self, user_id: str, task_id: str) -> TaskStatus:
        raise NotImplementedInPhase("orchestrator.get_status lands in Phase 4")

    def resume(self, task_id: str) -> TaskStatus:
        raise NotImplementedInPhase("orchestrator.resume lands in Phase 4")

    def stream_events(self, user_id: str, task_id: str) -> Iterator[TaskEvent]:
        raise NotImplementedInPhase("orchestrator.stream_events lands in Phase 4/6")
