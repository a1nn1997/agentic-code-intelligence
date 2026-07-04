"""The agent control loop: a hand-written plan -> retrieve -> edit -> verify ->
repair state machine, bounded by step and token budgets, with an append-only
journal that makes every run replayable.

This module is explicitly hand-owned — no framework hides the loop (prime
directive). Idempotency keys on ``(task_id, step_index)`` + patch applied-flags
give effectively-once semantics: a killed run resumes with no double-charge and
no re-applied side effect (Phase 4). Phase 0 defines the interface.
"""

from acp.orchestrator.interface import (
    Orchestrator,
    TaskEvent,
    TaskRequest,
    TaskStatus,
)
from acp.orchestrator.loop import AgentLoop, LoopConfig
from acp.orchestrator.service import OrchestratorImpl
from acp.orchestrator.stub import StubOrchestrator

__all__ = [
    "Orchestrator",
    "TaskRequest",
    "TaskStatus",
    "TaskEvent",
    "StubOrchestrator",
    "AgentLoop",
    "LoopConfig",
    "OrchestratorImpl",
]
