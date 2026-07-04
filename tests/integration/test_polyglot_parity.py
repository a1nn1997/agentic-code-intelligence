"""Phase-8 polyglot parity oracle.

Runs the SAME Phase-3 oracle clauses against the Go, Rust, and TypeScript
sandbox runners and asserts they return equivalent VerificationResults for the
same fixture inputs. Also asserts config-selection works (same task verifies
through each runner selected via SANDBOX_RUNNER setting).

Three honesty levels — per the Phase-8 spec:
  - Docker-parity proven: runner image exists + oracle ran against real Docker.
  - Implemented-but-not-docker-proven: runner implemented + unit-tested but
    image was not built in this env — explicitly flagged, never reported green.
  - Each runner's status is independently reported below.

Markers:
  pytest.mark.parity   — cross-runner parity tests (requires Docker + all images)
  pytest.mark.integration — depends on Docker daemon
  pytest.mark.docker   — requires specific runner images
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from tests.docker_gate import requires_sandbox

from acp.sandbox_client import fixtures
from acp.sandbox_client.docker_runner import SandboxLimits
from acp.sandbox_client.interface import (
    KilledReason,
    VerificationRequest,
    VerificationStage,
)
from acp.sandbox_client.polyglot_runners import (
    GoSandboxRunner,
    RustSandboxRunner,
    TsSandboxRunner,
)

pytestmark = [pytest.mark.parity, pytest.mark.integration, pytest.mark.docker]

# --- runner image availability ------------------------------------------------

_RUNNER_IMAGES = {
    "go": "acp-sandbox-go:latest",
    "rust": "acp-sandbox-rust:latest",
    "ts": "acp-sandbox-ts:latest",
}

_PYTHON_IMAGE = "acp-sandbox:latest"


def _image_available(image: str) -> bool:
    """True iff the Docker daemon is reachable AND the image exists locally."""
    try:
        r = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
            timeout=15,
        )
        return r.returncode == 0
    except Exception:
        return False


def _docker_available() -> bool:
    try:
        r = subprocess.run(["docker", "info"], capture_output=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return False


# Per-runner gate decorators. Each runner is skipped if its image is missing —
# UNLESS ACP_REQUIRE_DOCKER=1 (make eval-docker / test-docker) and the daemon is
# up, in which case a missing-but-buildable image FAILS loudly (A8): a skipped
# real-parity proof must never read as green when it was explicitly demanded.
_requires_go = requires_sandbox(_RUNNER_IMAGES["go"])
_requires_rust = requires_sandbox(_RUNNER_IMAGES["rust"])
_requires_ts = requires_sandbox(_RUNNER_IMAGES["ts"])
_requires_python_sandbox = requires_sandbox(_PYTHON_IMAGE)


def _req(patch: str) -> VerificationRequest:
    return VerificationRequest(
        task_id="parity-test", workspace_id="w", base_commit="c", patch=patch
    )


def _limits(**kw: object) -> SandboxLimits:
    defaults = dict(wall_clock_seconds=60, memory_mb=512)
    defaults.update(kw)
    return SandboxLimits(**defaults)  # type: ignore[arg-type]


def _count_acp_sandbox_containers() -> int:
    """Count running acp-sandbox-* containers (for leak proof)."""
    try:
        r = subprocess.run(
            ["docker", "ps", "--filter", "name=acp-sandbox-", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=10,
        )
        names = [n.strip() for n in r.stdout.splitlines() if n.strip()]
        return len(names)
    except Exception:
        return -1


# =============================================================================
# Phase-3 oracle clauses run against the Go runner
# =============================================================================

class TestGoRunner:
    """Phase-3 oracle clauses run against acp-sandbox-go:latest."""

    def _runner(self, **kw: object) -> GoSandboxRunner:
        return GoSandboxRunner(image=_RUNNER_IMAGES["go"], limits=_limits(**kw))

    @_requires_go
    def test_good_patch_verifies(self, sample_repo: Path) -> None:
        res = self._runner().verify_snapshot(_req(fixtures.good_patch()), sample_repo)
        assert res.applied is True
        assert res.built is True
        assert res.tests_passed is True
        assert res.exit_code == 0
        assert res.verified is True
        assert res.stage == VerificationStage.DONE
        assert res.killed_reason is None

    @_requires_go
    def test_bad_patch_returns_real_failure_text(self, sample_repo: Path) -> None:
        res = self._runner().verify_snapshot(_req(fixtures.bad_patch()), sample_repo)
        assert res.tests_passed is False
        assert res.verified is False
        combined = res.stdout_tail + res.stderr_tail
        assert fixtures.BAD_PATCH_MARKER in combined

    @_requires_go
    def test_network_egress_denied(self, sample_repo: Path) -> None:
        res = self._runner().verify_snapshot(_req(fixtures.network_patch()), sample_repo)
        assert res.tests_passed is False
        assert res.verified is False
        assert res.killed_reason == KilledReason.NETWORK

    @_requires_go
    def test_wall_clock_kill(self, sample_repo: Path) -> None:
        res = self._runner(wall_clock_seconds=5).verify_snapshot(
            _req(fixtures.infinite_loop_patch()), sample_repo
        )
        assert res.verified is False
        assert res.killed_reason == KilledReason.DEADLINE
        assert res.wall_clock_seconds >= 5
        assert res.wall_clock_seconds < 30

    @_requires_go
    def test_host_isolation(self, sample_repo: Path) -> None:
        import hashlib
        def tree_hash(root: Path) -> str:
            h = hashlib.sha256()
            for f in sorted(p for p in root.rglob("*") if p.is_file()):
                h.update(f.relative_to(root).as_posix().encode())
                h.update(b"\0")
                h.update(f.read_bytes())
            return h.hexdigest()
        before = tree_hash(sample_repo)
        self._runner().verify_snapshot(_req(fixtures.good_patch()), sample_repo)
        after = tree_hash(sample_repo)
        assert before == after, "Go runner mutated the host snapshot — isolation broken"

    @_requires_go
    def test_memory_limit_binds(self, sample_repo: Path) -> None:
        req = _req(fixtures.bounded_alloc_patch())
        high = self._runner(memory_mb=512, wall_clock_seconds=30).verify_snapshot(req, sample_repo)
        assert high.verified is True
        low = self._runner(memory_mb=128, wall_clock_seconds=30).verify_snapshot(req, sample_repo)
        assert low.verified is False
        assert low.killed_reason == KilledReason.OOM

    @_requires_go
    def test_zero_container_leak_after_timeout(self, sample_repo: Path) -> None:
        import time
        before = _count_acp_sandbox_containers()
        assert before >= 0
        runner = self._runner(wall_clock_seconds=3)
        res = runner.verify_snapshot(_req(fixtures.infinite_loop_patch()), sample_repo)
        assert res.killed_reason == KilledReason.DEADLINE
        time.sleep(1)
        after = _count_acp_sandbox_containers()
        assert after == before, (
            f"Go runner container leak: {after - before} container(s) still running "
            f"after timeout (before={before}, after={after})"
        )


# =============================================================================
# Phase-3 oracle clauses run against the Rust runner
# =============================================================================

class TestRustRunner:
    """Phase-3 oracle clauses run against acp-sandbox-rust:latest."""

    def _runner(self, **kw: object) -> RustSandboxRunner:
        return RustSandboxRunner(image=_RUNNER_IMAGES["rust"], limits=_limits(**kw))

    @_requires_rust
    def test_good_patch_verifies(self, sample_repo: Path) -> None:
        res = self._runner().verify_snapshot(_req(fixtures.good_patch()), sample_repo)
        assert res.applied is True
        assert res.built is True
        assert res.tests_passed is True
        assert res.exit_code == 0
        assert res.verified is True
        assert res.stage == VerificationStage.DONE
        assert res.killed_reason is None

    @_requires_rust
    def test_bad_patch_returns_real_failure_text(self, sample_repo: Path) -> None:
        res = self._runner().verify_snapshot(_req(fixtures.bad_patch()), sample_repo)
        assert res.tests_passed is False
        assert res.verified is False
        combined = res.stdout_tail + res.stderr_tail
        assert fixtures.BAD_PATCH_MARKER in combined

    @_requires_rust
    def test_network_egress_denied(self, sample_repo: Path) -> None:
        res = self._runner().verify_snapshot(_req(fixtures.network_patch()), sample_repo)
        assert res.tests_passed is False
        assert res.verified is False
        assert res.killed_reason == KilledReason.NETWORK

    @_requires_rust
    def test_wall_clock_kill(self, sample_repo: Path) -> None:
        res = self._runner(wall_clock_seconds=5).verify_snapshot(
            _req(fixtures.infinite_loop_patch()), sample_repo
        )
        assert res.verified is False
        assert res.killed_reason == KilledReason.DEADLINE
        assert res.wall_clock_seconds >= 5
        assert res.wall_clock_seconds < 30

    @_requires_rust
    def test_host_isolation(self, sample_repo: Path) -> None:
        import hashlib
        def tree_hash(root: Path) -> str:
            h = hashlib.sha256()
            for f in sorted(p for p in root.rglob("*") if p.is_file()):
                h.update(f.relative_to(root).as_posix().encode())
                h.update(b"\0")
                h.update(f.read_bytes())
            return h.hexdigest()
        before = tree_hash(sample_repo)
        self._runner().verify_snapshot(_req(fixtures.good_patch()), sample_repo)
        after = tree_hash(sample_repo)
        assert before == after, "Rust runner mutated the host snapshot — isolation broken"

    @_requires_rust
    def test_memory_limit_binds(self, sample_repo: Path) -> None:
        req = _req(fixtures.bounded_alloc_patch())
        high = self._runner(memory_mb=512, wall_clock_seconds=30).verify_snapshot(req, sample_repo)
        assert high.verified is True
        low = self._runner(memory_mb=128, wall_clock_seconds=30).verify_snapshot(req, sample_repo)
        assert low.verified is False
        assert low.killed_reason == KilledReason.OOM

    @_requires_rust
    def test_zero_container_leak_after_timeout(self, sample_repo: Path) -> None:
        import time
        before = _count_acp_sandbox_containers()
        assert before >= 0
        runner = self._runner(wall_clock_seconds=3)
        res = runner.verify_snapshot(_req(fixtures.infinite_loop_patch()), sample_repo)
        assert res.killed_reason == KilledReason.DEADLINE
        time.sleep(1)
        after = _count_acp_sandbox_containers()
        assert after == before, (
            f"Rust runner container leak: {after - before} container(s) still running "
            f"after timeout (before={before}, after={after})"
        )


# =============================================================================
# Phase-3 oracle clauses run against the TypeScript runner
# =============================================================================

class TestTsRunner:
    """Phase-3 oracle clauses run against acp-sandbox-ts:latest."""

    def _runner(self, **kw: object) -> TsSandboxRunner:
        return TsSandboxRunner(image=_RUNNER_IMAGES["ts"], limits=_limits(**kw))

    @_requires_ts
    def test_good_patch_verifies(self, sample_repo: Path) -> None:
        res = self._runner().verify_snapshot(_req(fixtures.good_patch()), sample_repo)
        assert res.applied is True
        assert res.built is True
        assert res.tests_passed is True
        assert res.exit_code == 0
        assert res.verified is True
        assert res.stage == VerificationStage.DONE
        assert res.killed_reason is None

    @_requires_ts
    def test_bad_patch_returns_real_failure_text(self, sample_repo: Path) -> None:
        res = self._runner().verify_snapshot(_req(fixtures.bad_patch()), sample_repo)
        assert res.tests_passed is False
        assert res.verified is False
        combined = res.stdout_tail + res.stderr_tail
        assert fixtures.BAD_PATCH_MARKER in combined

    @_requires_ts
    def test_network_egress_denied(self, sample_repo: Path) -> None:
        res = self._runner().verify_snapshot(_req(fixtures.network_patch()), sample_repo)
        assert res.tests_passed is False
        assert res.verified is False
        assert res.killed_reason == KilledReason.NETWORK

    @_requires_ts
    def test_wall_clock_kill(self, sample_repo: Path) -> None:
        res = self._runner(wall_clock_seconds=5).verify_snapshot(
            _req(fixtures.infinite_loop_patch()), sample_repo
        )
        assert res.verified is False
        assert res.killed_reason == KilledReason.DEADLINE
        assert res.wall_clock_seconds >= 5
        assert res.wall_clock_seconds < 30

    @_requires_ts
    def test_host_isolation(self, sample_repo: Path) -> None:
        import hashlib
        def tree_hash(root: Path) -> str:
            h = hashlib.sha256()
            for f in sorted(p for p in root.rglob("*") if p.is_file()):
                h.update(f.relative_to(root).as_posix().encode())
                h.update(b"\0")
                h.update(f.read_bytes())
            return h.hexdigest()
        before = tree_hash(sample_repo)
        self._runner().verify_snapshot(_req(fixtures.good_patch()), sample_repo)
        after = tree_hash(sample_repo)
        assert before == after, "TS runner mutated the host snapshot — isolation broken"

    @_requires_ts
    def test_memory_limit_binds(self, sample_repo: Path) -> None:
        req = _req(fixtures.bounded_alloc_patch())
        high = self._runner(memory_mb=512, wall_clock_seconds=30).verify_snapshot(req, sample_repo)
        assert high.verified is True
        low = self._runner(memory_mb=128, wall_clock_seconds=30).verify_snapshot(req, sample_repo)
        assert low.verified is False
        assert low.killed_reason == KilledReason.OOM

    @_requires_ts
    def test_zero_container_leak_after_timeout(self, sample_repo: Path) -> None:
        import time
        before = _count_acp_sandbox_containers()
        assert before >= 0
        runner = self._runner(wall_clock_seconds=3)
        res = runner.verify_snapshot(_req(fixtures.infinite_loop_patch()), sample_repo)
        assert res.killed_reason == KilledReason.DEADLINE
        time.sleep(1)
        after = _count_acp_sandbox_containers()
        assert after == before, (
            f"TS runner container leak: {after - before} container(s) still running "
            f"after timeout (before={before}, after={after})"
        )


# =============================================================================
# Cross-runner parity assertion: all available runners must agree on fixtures
# =============================================================================

class TestCrossRunnerParity:
    """Assert available runners return equivalent VerificationResults for the
    same fixtures. Any unavailable runner is skipped per the honesty gate; only
    runners with images present participate in the cross-assertion."""

    @pytest.mark.skipif(
        not _docker_available(),
        reason="Docker daemon not available",
    )
    def test_parity_good_patch(self, sample_repo: Path) -> None:
        """All available runners agree: good patch is verified."""
        results = {}
        runner_map = {
            "go": (GoSandboxRunner, _RUNNER_IMAGES["go"]),
            "rust": (RustSandboxRunner, _RUNNER_IMAGES["rust"]),
            "ts": (TsSandboxRunner, _RUNNER_IMAGES["ts"]),
        }
        for name, (cls, image) in runner_map.items():
            if not _image_available(image):
                continue
            runner = cls(image=image, limits=_limits())
            results[name] = runner.verify_snapshot(_req(fixtures.good_patch()), sample_repo)

        if len(results) < 2:
            pytest.skip(
                f"Only {len(results)} runner image(s) available for cross-parity. "
                "Build all three images to run this assertion."
            )

        # All must agree: verified=True, tests_passed=True, exit_code=0
        for name, res in results.items():
            assert res.verified is True, f"{name} runner: verified should be True"
            assert res.tests_passed is True, f"{name} runner: tests_passed should be True"
            assert res.exit_code == 0, f"{name} runner: exit_code should be 0"

    @pytest.mark.skipif(
        not _docker_available(),
        reason="Docker daemon not available",
    )
    def test_parity_bad_patch(self, sample_repo: Path) -> None:
        """All available runners agree: bad patch fails with the known marker."""
        results = {}
        runner_map = {
            "go": (GoSandboxRunner, _RUNNER_IMAGES["go"]),
            "rust": (RustSandboxRunner, _RUNNER_IMAGES["rust"]),
            "ts": (TsSandboxRunner, _RUNNER_IMAGES["ts"]),
        }
        for name, (cls, image) in runner_map.items():
            if not _image_available(image):
                continue
            runner = cls(image=image, limits=_limits())
            results[name] = runner.verify_snapshot(_req(fixtures.bad_patch()), sample_repo)

        if len(results) < 2:
            pytest.skip(
                f"Only {len(results)} runner image(s) available for cross-parity. "
                "Build all three images to run this assertion."
            )

        for name, res in results.items():
            assert res.verified is False, f"{name} runner: verified should be False"
            combined = res.stdout_tail + res.stderr_tail
            assert fixtures.BAD_PATCH_MARKER in combined, (
                f"{name} runner: BAD_PATCH_MARKER not in output — real failure text not captured"
            )


# =============================================================================
# Config-selection proof: SANDBOX_RUNNER routes to the correct runner class
# =============================================================================

class TestConfigSelection:
    """Prove that build_sandbox_client routes to the correct runner class based
    on the SANDBOX_RUNNER setting — NO caller code change needed to switch."""

    def test_python_runner_is_default(self) -> None:
        from acp.config.settings import SandboxRunner, Settings
        from acp.sandbox_client import build_sandbox_client
        from acp.sandbox_client.docker_runner import DockerSandboxRunner

        s = Settings(
            sandbox_runner=SandboxRunner.PYTHON,
            database_url="sqlite:///./var/acp.db",
        )
        client = build_sandbox_client(s)
        assert isinstance(client, DockerSandboxRunner)
        # Must NOT be a subclass (Go/Rust/TS all subclass DockerSandboxRunner).
        assert type(client) is DockerSandboxRunner

    def test_go_runner_selected_by_config(self) -> None:
        from acp.config.settings import SandboxRunner, Settings
        from acp.sandbox_client import build_sandbox_client
        from acp.sandbox_client.polyglot_runners import GoSandboxRunner

        s = Settings(
            sandbox_runner=SandboxRunner.GO,
            database_url="sqlite:///./var/acp.db",
        )
        client = build_sandbox_client(s)
        assert isinstance(client, GoSandboxRunner)

    def test_rust_runner_selected_by_config(self) -> None:
        from acp.config.settings import SandboxRunner, Settings
        from acp.sandbox_client import build_sandbox_client
        from acp.sandbox_client.polyglot_runners import RustSandboxRunner

        s = Settings(
            sandbox_runner=SandboxRunner.RUST,
            database_url="sqlite:///./var/acp.db",
        )
        client = build_sandbox_client(s)
        assert isinstance(client, RustSandboxRunner)

    def test_ts_runner_selected_by_config(self) -> None:
        from acp.config.settings import SandboxRunner, Settings
        from acp.sandbox_client import build_sandbox_client
        from acp.sandbox_client.polyglot_runners import TsSandboxRunner

        s = Settings(
            sandbox_runner=SandboxRunner.TS,
            database_url="sqlite:///./var/acp.db",
        )
        client = build_sandbox_client(s)
        assert isinstance(client, TsSandboxRunner)
