"""Interface contract for the sandbox tier — the verification oracle.

``SANDBOX_RUNNER`` selects which conforming runner services a job (Python is the
Phase-3 reference; Go/Rust/TS are Phase-8, parity-tested against the same
oracle). The client interface is runner-agnostic by design: swapping runners is
a config change, never a code change (ADR-003, "own & swap the core").
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field


class VerificationStage(StrEnum):
    """Where the pipeline stopped. Monotone: apply -> build -> test -> done."""

    APPLY = "apply"
    BUILD = "build"
    TEST = "test"
    DONE = "done"


class KilledReason(StrEnum):
    """Why a run was terminated before it could produce a normal verdict.

    ``DEADLINE`` — wall-clock kill (host-side timeout -> docker kill).
    ``OOM``      — the kernel OOM-killed the container (cgroup memory limit).
    ``NETWORK``  — a build/test step attempted egress; it failed closed because
                   the container has no network. (Surfaced from the captured
                   error text, so a phone-home is reported, never a false pass.)
    ``ERROR``    — the sandbox infrastructure itself failed (image missing, docker
                   unreachable): distinct from a legitimate test failure.
    """

    DEADLINE = "deadline"
    OOM = "oom"
    NETWORK = "network"
    ERROR = "error"


class VerificationRequest(BaseModel):
    """A unit of untrusted work to execute in isolation.

    ``patch`` is an owned JSON envelope of whole-file operations
    ``{"ops": [{"op": "write"|"delete", "path": ..., "content": ...}]}`` — a
    tool-free, deterministic format so the model-free oracle is reproducible and
    every conforming runner (Phase 8) applies patches identically. Span/diff
    translation lives above this boundary (Phase 4), never inside the sandbox.
    """

    task_id: str
    workspace_id: str
    base_commit: str
    patch: str = Field(description="Owned JSON patch envelope to apply before building")
    runner: str = Field(default="python", description="python | go | rust | ts")


class VerificationResult(BaseModel):
    """The oracle's verdict — the single source of truth for "done".

    STABLE CONTRACT: Phase 8's Go/Rust/TS runners MUST populate this exact shape;
    Phase 4's agent loop and Phase 5's self-repair read ONLY these fields and
    never a model self-report. ``verified`` is true iff the patch applied AND the
    build passed AND the tests passed AND no kill occurred — all in the sandbox.

    On any kill (``killed_reason`` set), ``verified`` is false by construction and
    ``stderr_tail`` carries the real reason, so a timeout or a phone-home can never
    masquerade as a pass.
    """

    verified: bool
    applied: bool
    built: bool
    tests_passed: bool
    exit_code: int
    stage: VerificationStage = Field(
        default=VerificationStage.APPLY, description="Furthest stage reached"
    )
    stdout_tail: str = Field(default="", description="Truncated, redacted build/test output")
    stderr_tail: str = Field(default="", description="Real failure text for the repair loop")
    wall_clock_seconds: float = 0.0
    killed_reason: KilledReason | None = Field(
        default=None, description="Set iff the run was terminated early; see KilledReason"
    )


@runtime_checkable
class SandboxClient(Protocol):
    """Submit untrusted work; get back the structured oracle verdict."""

    def verify(self, request: VerificationRequest) -> VerificationResult:
        """Apply -> build -> test in an isolated container; return the verdict.

        Resolves the snapshot from ``request.base_commit`` via the workspace's
        worktree (wired in Phase 4).
        """
        ...

    def verify_snapshot(
        self, request: VerificationRequest, snapshot_dir: Path
    ) -> VerificationResult:
        """Verify against an explicit, already-materialized snapshot directory.

        The snapshot is treated as read-only input; a run never mutates it (nor
        the host). This is the Phase-3 entrypoint the operator CLI and tests use;
        Phase 8 runners MUST implement it with identical semantics.
        """
        ...

    def healthy(self) -> bool:
        """Whether the sandbox host is reachable and able to accept jobs.

        Used by the gateway's ``/readyz`` — the control plane is not *ready* to
        promise verified changes if the sandbox tier is down.
        """
        ...
