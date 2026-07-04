"""Control-plane client to the isolated sandbox tier.

The sandbox is a *contract*: given a patch + a workspace snapshot, apply it,
build/type-check, run the repo's tests inside Docker (``--network=none``,
cgroups, wall-clock kill, dropped caps, non-root, read-only rootfs), and return
a structured result. That result — never a model self-report — is the oracle for
"is this change actually done" (Phase 3). Phase 0 defines the interface.
"""

from typing import TYPE_CHECKING

from acp.sandbox_client.docker_runner import DockerSandboxRunner, SandboxLimits
from acp.sandbox_client.interface import (
    KilledReason,
    SandboxClient,
    VerificationRequest,
    VerificationResult,
    VerificationStage,
)
from acp.sandbox_client.polyglot_runners import GoSandboxRunner, RustSandboxRunner, TsSandboxRunner
from acp.sandbox_client.stub import StubSandboxClient

if TYPE_CHECKING:
    from acp.config import Settings


def build_sandbox_client(settings: "Settings | None" = None) -> SandboxClient:
    """Construct the configured sandbox client.

    ``SANDBOX_RUNNER`` selects the runner; ``python`` is the Phase-3 reference.
    Phase-8 runners (go/rust/ts) conform to the same SandboxClient protocol and
    are selected here with NO caller code change — only a config env var.
    """
    from acp.config import get_settings
    from acp.config.settings import SandboxRunner

    s = settings or get_settings()
    limits = SandboxLimits(
        cpus=s.sandbox_cpus,
        memory_mb=s.sandbox_memory_mb,
        tmpfs_mb=s.sandbox_tmpfs_mb,
        wall_clock_seconds=s.sandbox_wall_clock_seconds,
        pids=s.sandbox_pids,
    )
    # Runner-selection seam: SANDBOX_RUNNER env var routes to the chosen runner.
    # All runners share the same SandboxClient interface; callers are unaffected.
    runner = s.sandbox_runner
    if runner == SandboxRunner.GO:
        return GoSandboxRunner(image="acp-sandbox-go:latest", limits=limits)
    if runner == SandboxRunner.RUST:
        return RustSandboxRunner(image="acp-sandbox-rust:latest", limits=limits)
    if runner == SandboxRunner.TS:
        return TsSandboxRunner(image="acp-sandbox-ts:latest", limits=limits)
    # Default: python (Phase-3 reference runner).
    return DockerSandboxRunner(image=s.sandbox_image, limits=limits)


__all__ = [
    "build_sandbox_client",
    "SandboxClient",
    "StubSandboxClient",
    "DockerSandboxRunner",
    "GoSandboxRunner",
    "RustSandboxRunner",
    "TsSandboxRunner",
    "SandboxLimits",
    "VerificationRequest",
    "VerificationResult",
    "VerificationStage",
    "KilledReason",
]
