"""Python reference sandbox runner — real Docker isolation (Phase 3).

This is the host-side half of the sandbox. It prepares an isolated copy of the
workspace snapshot, launches the in-container :mod:`sandbox.entrypoint` inside a
locked-down Docker container, enforces a wall-clock deadline with a hard kill,
and parses the container's structured result into a
:class:`~acp.sandbox_client.interface.VerificationResult`.

Every isolation property is enforced by a concrete Docker run flag here (not by
config elsewhere), and each is proven by a Phase-3 test that exercises the real
mechanism:

    property     | mechanism (this file)                       | proven by
    -------------|---------------------------------------------|--------------------------
    network      | ``--network none``                          | egress attempt fails closed
    cpu          | ``--cpus``                                  | (configured; see DESIGN)
    memory       | ``--memory`` + ``--memory-swap`` (= no swap)| OOM => killed_reason=oom
    disk         | ``--read-only`` rootfs + ``--tmpfs /work``  | writes land only in tmpfs
    wall-clock   | host ``communicate(timeout=)`` -> ``docker kill`` | infinite loop killed
    user         | ``--user 10001:10001`` (non-root)           | (image + flag)
    caps         | ``--cap-drop ALL``                          | (flag)
    no-new-privs | ``--security-opt no-new-privileges``        | (flag)
    rootfs       | ``--read-only``                             | host tree unmodified after run

The host tree is NEVER mounted writable: the snapshot is bind-mounted read-only
and copied into tmpfs inside the container, so a run cannot mutate the host.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from acp.common.logging import get_logger
from acp.retrieval.redaction import redact_secrets
from acp.sandbox_client.interface import (
    KilledReason,
    VerificationRequest,
    VerificationResult,
    VerificationStage,
)

_log = get_logger(__name__)

# Must match sandbox/entrypoint.py.
_RESULT_BEGIN = "<<<ACP_RESULT_BEGIN>>>"
_RESULT_END = "<<<ACP_RESULT_END>>>"

# Substrings that betray an egress attempt in captured failure text. A test that
# tries to open a socket with --network=none fails with one of these; we map it
# to the explicit NETWORK kill state so a phone-home is reported, never passed.
_NETWORK_ERROR_MARKERS = (
    "Network is unreachable",
    "Temporary failure in name resolution",
    "Name or service not known",
    "Errno -3",
    "Errno -2",
    "Errno 101",
    "nodename nor servname provided",
    "Connection refused",
    "getaddrinfo",
)


@dataclass(frozen=True)
class SandboxLimits:
    """Resource ceilings applied to every run. Chosen conservative-but-usable;
    threaded from settings so operators can tune without code changes."""

    cpus: float = 1.0
    memory_mb: int = 512
    tmpfs_mb: int = 256
    wall_clock_seconds: int = 60
    pids: int = 256


class DockerSandboxRunner:
    """Conforms to :class:`acp.sandbox_client.interface.SandboxClient`.

    All executed code is treated as untrusted; the only trust we place is in the
    Docker daemon's enforcement of the run flags below.
    """

    def __init__(
        self,
        image: str = "acp-sandbox:latest",
        limits: SandboxLimits | None = None,
        docker_bin: str = "docker",
    ) -> None:
        self._image = image
        self._limits = limits or SandboxLimits()
        self._docker = docker_bin

    # --- health --------------------------------------------------------------
    def healthy(self) -> bool:
        """The sandbox tier is healthy iff the Docker daemon answers and the
        runner image exists (so a job would actually be runnable)."""
        try:
            info = subprocess.run(
                [self._docker, "image", "inspect", self._image],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return info.returncode == 0
        except Exception as exc:  # noqa: BLE001 - readiness must not raise
            _log.warning("sandbox.health_check_failed", extra={"detail": str(exc)})
            return False

    # --- the oracle ----------------------------------------------------------
    def verify(self, request: VerificationRequest) -> VerificationResult:
        """Apply -> build -> test inside an isolated container; return the verdict.

        ``base_commit`` selects nothing here (the caller hands us a materialized
        snapshot dir via :meth:`verify_snapshot`); this overload exists for the
        stable Protocol. Phase 4 wires worktrees; for Phase 3 the operator/tests
        call :meth:`verify_snapshot` with an explicit path.
        """
        raise NotImplementedError(
            "Phase 3 uses verify_snapshot(request, snapshot_dir); worktree wiring is Phase 4"
        )

    def verify_snapshot(
        self, request: VerificationRequest, snapshot_dir: Path
    ) -> VerificationResult:
        """Run the pipeline against an explicit, already-materialized snapshot.

        ``snapshot_dir`` is bind-mounted READ-ONLY; the container copies it into
        its tmpfs before touching anything, so this directory (and the host) is
        never mutated by the run.
        """
        snapshot_dir = snapshot_dir.resolve()
        if not snapshot_dir.is_dir():
            return self._infra_error(f"snapshot dir does not exist: {snapshot_dir}")

        # Stage the patch envelope in a throwaway host dir, bind-mounted RO.
        with tempfile.TemporaryDirectory(prefix="acp-patch-") as patch_tmp:
            patch_dir = Path(patch_tmp)
            (patch_dir / "patch.json").write_text(request.patch, encoding="utf-8")
            return self._run_container(snapshot_dir, patch_dir)

    # --- internals -----------------------------------------------------------
    def _docker_args(self, snapshot_dir: Path, patch_dir: Path, name: str) -> list[str]:
        """The exact `docker run` invocation — every isolation flag lives here.

        ``name`` is a per-run unique identifier so we can explicitly stop/rm the
        container on timeout or any error path. This is the root-fix for the
        Phase-4 container leak: killing the docker-run *client* process does not
        guarantee the container stops (Docker's --rm only fires on normal exit).
        With an explicit name we can always ``docker stop <name> && docker rm <name``.
        """
        lim = self._limits
        return [
            self._docker, "run", "--rm",
            # unique name so we can stop/rm this exact container on any error path.
            "--name", name,
            # network: hard egress denial. Nothing the container runs can reach out.
            "--network", "none",
            # cpu + memory (cgroups). memory-swap == memory => swap disabled, so an
            # over-limit allocation is OOM-killed rather than silently swapping.
            "--cpus", str(lim.cpus),
            "--memory", f"{lim.memory_mb}m",
            "--memory-swap", f"{lim.memory_mb}m",
            # pid bomb ceiling.
            "--pids-limit", str(lim.pids),
            # rootfs read-only; the ONLY writable path is a size-capped tmpfs.
            "--read-only",
            "--tmpfs", f"/work:rw,size={lim.tmpfs_mb}m,mode=1777",
            # non-root + drop every capability + no privilege escalation.
            "--user", "10001:10001",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            # snapshot + patch mounted READ-ONLY. Host tree cannot be written.
            "--mount", f"type=bind,src={snapshot_dir},dst=/snapshot,ro",
            "--mount", f"type=bind,src={patch_dir},dst=/patch,ro",
            self._image,
        ]

    def _force_remove_container(self, name: str) -> None:
        """Explicitly stop and remove a named container.

        Called on EVERY error/timeout path — this is the source-level container
        leak fix. ``docker stop`` sends SIGTERM then waits; ``docker rm -f``
        forces removal even if stop did not finish. Best-effort: never raises.
        The Makefile ``sandbox-clean`` trap remains as defense-in-depth.
        """
        for cmd in (
            [self._docker, "stop", "--time", "5", name],
            [self._docker, "rm", "-f", name],
        ):
            try:
                subprocess.run(cmd, capture_output=True, timeout=15)
            except Exception:  # noqa: BLE001
                pass

    def _run_container(self, snapshot_dir: Path, patch_dir: Path) -> VerificationResult:
        # Unique per-run name so we can stop/rm this container explicitly on any
        # error path — the root fix for the Phase-4 container leak.
        run_name = f"acp-sandbox-{uuid.uuid4().hex[:12]}"
        args = self._docker_args(snapshot_dir, patch_dir, run_name)
        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
        except FileNotFoundError:
            return self._infra_error("docker binary not found on host")

        try:
            out, err = proc.communicate(timeout=self._limits.wall_clock_seconds)
        except subprocess.TimeoutExpired:
            # Wall-clock breach: hard-kill the container at the SOURCE (not just
            # the client process). Sequence: kill client → stop named container →
            # rm named container. This is the root-level leak fix: --rm only fires
            # on normal exit; a killed client can leave the container running.
            wall = round(time.monotonic() - started, 3)
            proc.kill()
            try:
                proc.communicate(timeout=10)
            except Exception:  # noqa: BLE001
                pass
            self._force_remove_container(run_name)
            _log.warning("sandbox.deadline_kill", extra={"wall_clock_seconds": wall})
            return VerificationResult(
                verified=False,
                applied=False,
                built=False,
                tests_passed=False,
                exit_code=-1,
                stage=VerificationStage.TEST,
                stderr_tail=redact_secrets(
                    f"killed: wall-clock deadline of "
                    f"{self._limits.wall_clock_seconds}s exceeded"
                ),
                wall_clock_seconds=wall,
                killed_reason=KilledReason.DEADLINE,
            )

        wall = round(time.monotonic() - started, 3)
        result = self._parse(out or "", err or "", proc.returncode, wall)
        # On infra-error paths the container may still be running; clean it up.
        if result.killed_reason == KilledReason.ERROR:
            self._force_remove_container(run_name)
        return result

    def _parse(
        self, out: str, err: str, docker_rc: int, wall: float
    ) -> VerificationResult:
        """Turn the container's sentinel-delimited JSON into a VerificationResult.

        Also classifies OOM (docker rc 137 / kill signal) and network-attempt
        (error markers) into explicit killed states — those must never look like
        an ordinary test failure or, worse, a pass.
        """
        payload = self._extract_payload(out)

        # OOM: the kernel killed the container. With swap disabled and a memory
        # cgroup, an over-allocation is SIGKILL'd. Depending on the docker
        # backend this surfaces as rc 137 (128+9) on the `docker run` client, or
        # as a negative signal (-9) when the client itself receives the signal.
        if payload is None and docker_rc in (137, -9):
            return VerificationResult(
                verified=False, applied=False, built=False, tests_passed=False,
                exit_code=docker_rc, stage=VerificationStage.TEST,
                stderr_tail=redact_secrets("killed: container OOM (memory limit)"),
                wall_clock_seconds=wall, killed_reason=KilledReason.OOM,
            )
        if payload is None:
            # No parseable result and not a recognized kill => infrastructure
            # failure (image missing, daemon error). Report, don't crash.
            return self._infra_error(
                f"no result payload (docker rc={docker_rc}): "
                f"{redact_secrets((err or out)[:2000])}",
                wall=wall,
            )

        stdout_tail = redact_secrets(str(payload.get("stdout_tail", "")))
        stderr_tail = redact_secrets(str(payload.get("stderr_tail", "")))
        applied = bool(payload.get("applied"))
        built = bool(payload.get("built"))
        tests_passed = bool(payload.get("tests_passed"))
        exit_code = int(str(payload.get("exit_code", -1)))
        stage = VerificationStage(str(payload.get("stage", "apply")))
        run_wall = float(str(payload.get("wall_clock_seconds", wall)))

        # A step that tried to phone home fails with a network error under
        # --network=none. Detect it in the REAL captured text and mark it an
        # explicit egress-denied kill: never a pass, and distinguishable from a
        # logic-level test failure.
        killed_reason: KilledReason | None = None
        combined = stdout_tail + "\n" + stderr_tail
        if not tests_passed and any(m in combined for m in _NETWORK_ERROR_MARKERS):
            killed_reason = KilledReason.NETWORK
        # OOM: the memory cgroup (with swap disabled) SIGKILLs the offending test
        # process, so the test step exits with signal 9 (-9) while producing no
        # normal failure text. That is the cgroup memory limit taking effect.
        elif not tests_passed and exit_code == -9 and not combined.strip():
            killed_reason = KilledReason.OOM

        verified = applied and built and tests_passed and killed_reason is None
        return VerificationResult(
            verified=verified,
            applied=applied,
            built=built,
            tests_passed=tests_passed,
            exit_code=exit_code,
            stage=VerificationStage.DONE if verified else stage,
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
            wall_clock_seconds=run_wall,
            killed_reason=killed_reason,
        )

    @staticmethod
    def _extract_payload(out: str) -> dict[str, object] | None:
        if _RESULT_BEGIN not in out or _RESULT_END not in out:
            return None
        # last occurrence wins, so untrusted test output printing the sentinel
        # earlier cannot spoof the real trailing result.
        body = out.rsplit(_RESULT_BEGIN, 1)[1].split(_RESULT_END, 1)[0].strip()
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def _infra_error(self, detail: str, wall: float = 0.0) -> VerificationResult:
        _log.error("sandbox.infra_error", extra={"detail": detail})
        return VerificationResult(
            verified=False, applied=False, built=False, tests_passed=False,
            exit_code=-1, stage=VerificationStage.APPLY,
            stderr_tail=redact_secrets(detail),
            wall_clock_seconds=wall, killed_reason=KilledReason.ERROR,
        )
