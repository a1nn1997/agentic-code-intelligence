"""Phase-0 stub for the sandbox client. Real Docker isolation lands in Phase 3.

``healthy()`` returns True because the in-process stub is trivially reachable —
this lets ``/readyz`` return 200 in keyless stub mode (Phase 0 oracle) without
pretending a real sandbox exists. ``verify()`` fails loudly.
"""

from __future__ import annotations

from pathlib import Path

from acp.common.errors import NotImplementedInPhase
from acp.sandbox_client.interface import VerificationRequest, VerificationResult


class StubSandboxClient:
    """Satisfies :class:`acp.sandbox_client.interface.SandboxClient` structurally."""

    def verify(self, request: VerificationRequest) -> VerificationResult:
        raise NotImplementedInPhase("sandbox verification lands in Phase 3")

    def verify_snapshot(
        self, request: VerificationRequest, snapshot_dir: Path
    ) -> VerificationResult:
        raise NotImplementedInPhase("sandbox verification lands in Phase 3")

    def healthy(self) -> bool:
        # The stub is reachable in-process; real reachability check is Phase 3.
        return True
