"""Phase-3 oracle: real sandboxed verification, proven by the ACTUAL mechanism.

Each test maps to one clause of the Phase-3 oracle and evidences it through the
real behaviour, never a proxy:

  1. good patch  -> applied=built=tests_passed=true, exit 0, verified
  2. bad patch   -> tests_passed=false AND the REAL error text is present
  3. network     -> a real socket connect fails closed (egress denied)
  4. wall-clock  -> an infinite loop is KILLED at the deadline; caller not hung
  5. host isol.  -> the host snapshot dir is byte-identical after a run
  6. memory      -> the same workload passes under a high cap, OOM-killed under a
                    low cap (the cgroup limit demonstrably binds)

These require a running Docker daemon and the acp-sandbox image; they are marked
``docker`` + ``integration``. If the image is missing they are skipped, so the
non-Docker suite stays green — UNLESS ``ACP_REQUIRE_DOCKER=1`` (set by
``make test-docker`` / ``make eval-docker``), in which case a missing-but-
buildable image FAILS loudly rather than skipping (A8), so the real-sandbox proof
can never be silently absent.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest
from tests.docker_gate import requires_sandbox as _requires_sandbox

from acp.sandbox_client import fixtures
from acp.sandbox_client.docker_runner import DockerSandboxRunner, SandboxLimits
from acp.sandbox_client.interface import (
    KilledReason,
    VerificationRequest,
    VerificationStage,
)

pytestmark = [pytest.mark.integration, pytest.mark.docker]

_IMAGE = "acp-sandbox:latest"


def requires_sandbox(func: object) -> object:
    """Gate this test on the real sandbox image (A8: fail-not-skip when the
    image is buildable but ``ACP_REQUIRE_DOCKER=1``)."""
    return _requires_sandbox(_IMAGE)(func)  # type: ignore[arg-type]


def _tree_hash(root: Path) -> str:
    """Content hash of an entire tree — path + bytes of every file."""
    h = hashlib.sha256()
    for f in sorted(p for p in root.rglob("*") if p.is_file()):
        h.update(f.relative_to(root).as_posix().encode("utf-8"))
        h.update(b"\0")
        h.update(f.read_bytes())
    return h.hexdigest()


@pytest.fixture
def snapshot(sample_repo: Path) -> Path:
    """A materialized snapshot dir (the sandbox bind-mounts this read-only)."""
    return sample_repo


def _req(patch: str) -> VerificationRequest:
    return VerificationRequest(
        task_id="t", workspace_id="w", base_commit="c", patch=patch
    )


def _runner(**overrides: object) -> DockerSandboxRunner:
    base = dict(wall_clock_seconds=60, memory_mb=512)
    base.update(overrides)
    return DockerSandboxRunner(image=_IMAGE, limits=SandboxLimits(**base))  # type: ignore[arg-type]


# --- clause 1: known-good patch builds and passes --------------------------------
@requires_sandbox
def test_good_patch_verifies(snapshot: Path) -> None:
    res = _runner().verify_snapshot(_req(fixtures.good_patch()), snapshot)
    assert res.applied is True
    assert res.built is True
    assert res.tests_passed is True
    assert res.exit_code == 0
    assert res.verified is True
    assert res.stage == VerificationStage.DONE
    assert res.killed_reason is None


# --- clause 2: known-bad patch surfaces the REAL failure text --------------------
@requires_sandbox
def test_bad_patch_returns_real_failure_text(snapshot: Path) -> None:
    res = _runner().verify_snapshot(_req(fixtures.bad_patch()), snapshot)
    assert res.applied is True
    assert res.built is True  # it compiles; it just fails at test time
    assert res.tests_passed is False
    assert res.verified is False
    # The linchpin: the ACTUAL captured error text is present, not a generic flag.
    # This is exactly what Phase-5 self-repair reads.
    combined = res.stdout_tail + res.stderr_tail
    assert fixtures.BAD_PATCH_MARKER in combined
    assert "test_known_bad_serialize" in combined


# --- stage gates: apply and build fail independently, with real text ------------
@requires_sandbox
def test_unapplyable_patch_fails_at_apply(snapshot: Path) -> None:
    res = _runner().verify_snapshot(_req(fixtures.unapplyable_patch()), snapshot)
    assert res.applied is False
    assert res.built is False
    assert res.tests_passed is False
    assert res.verified is False
    assert res.stage == VerificationStage.APPLY
    assert "escapes" in res.stderr_tail


@requires_sandbox
def test_unbuildable_patch_fails_at_build_with_syntaxerror(snapshot: Path) -> None:
    res = _runner().verify_snapshot(_req(fixtures.unbuildable_patch()), snapshot)
    assert res.applied is True
    assert res.built is False
    assert res.tests_passed is False
    assert res.verified is False
    assert res.stage == VerificationStage.BUILD
    assert "SyntaxError" in res.stderr_tail


# --- clause 3: network egress fails closed ---------------------------------------
@requires_sandbox
def test_network_egress_denied(snapshot: Path) -> None:
    res = _runner().verify_snapshot(_req(fixtures.network_patch()), snapshot)
    # Must NEVER be a pass: the connect cannot succeed with --network=none.
    assert res.tests_passed is False
    assert res.verified is False
    # Reported explicitly as an egress-denied kill, by the real error, not a proxy.
    assert res.killed_reason == KilledReason.NETWORK


# --- clause 4: wall-clock kill of an infinite loop -------------------------------
@requires_sandbox
def test_infinite_loop_killed_at_deadline(snapshot: Path) -> None:
    # Short deadline so the test itself is fast; the loop would run forever.
    res = _runner(wall_clock_seconds=5).verify_snapshot(
        _req(fixtures.infinite_loop_patch()), snapshot
    )
    assert res.tests_passed is False
    assert res.verified is False
    assert res.killed_reason == KilledReason.DEADLINE
    # Caller was not hung: it returned, and near the deadline (not much beyond).
    assert res.wall_clock_seconds >= 5
    assert res.wall_clock_seconds < 30
    assert "deadline" in res.stderr_tail.lower()


# --- clause 5: host isolation — the host tree is unmodified ----------------------
@requires_sandbox
def test_host_filesystem_unmodified_after_run(snapshot: Path) -> None:
    before = _tree_hash(snapshot)
    # Run a patch that WRITES files (inside the container) + one that fails.
    _runner().verify_snapshot(_req(fixtures.good_patch()), snapshot)
    _runner().verify_snapshot(_req(fixtures.bad_patch()), snapshot)
    after = _tree_hash(snapshot)
    assert before == after, "sandbox run mutated the host snapshot — isolation broken"


@requires_sandbox
def test_patch_files_never_appear_on_host(snapshot: Path) -> None:
    # The good patch writes backend/tests/test_known_good.py *inside* the
    # container's tmpfs copy; it must NOT exist on the host snapshot.
    _runner().verify_snapshot(_req(fixtures.good_patch()), snapshot)
    assert not (snapshot / "backend/tests/test_known_good.py").exists()


# --- clause 6: memory limit demonstrably binds -----------------------------------
@requires_sandbox
def test_memory_limit_binds(snapshot: Path) -> None:
    req = _req(fixtures.bounded_alloc_patch())
    # Same fixed ~200MB workload. Passes under a generous cap...
    high = _runner(memory_mb=512, wall_clock_seconds=30).verify_snapshot(req, snapshot)
    assert high.verified is True, "200MB alloc should fit under a 512MB cap"
    # ...and is OOM-killed under a cap below the workload. The LIMIT is what
    # decides, not the workload — that is the cgroup taking effect.
    low = _runner(memory_mb=128, wall_clock_seconds=30).verify_snapshot(req, snapshot)
    assert low.verified is False
    assert low.killed_reason == KilledReason.OOM


# --- contract sanity: the runner uses the isolated snapshot, not the host --------
@requires_sandbox
def test_empty_patch_runs_repo_own_tests(snapshot: Path) -> None:
    # No patch: the repo's OWN suite runs in the sandbox and passes.
    res = _runner().verify_snapshot(_req(fixtures.empty_patch()), snapshot)
    assert res.applied is True
    assert res.verified is True


@requires_sandbox
def test_infra_error_when_image_missing(snapshot: Path) -> None:
    # A bogus image name => infrastructure error state, NOT a crash, NOT a pass.
    runner = DockerSandboxRunner(image="acp-sandbox-does-not-exist:latest")
    assert runner.healthy() is False
    res = runner.verify_snapshot(_req(fixtures.empty_patch()), snapshot)
    assert res.verified is False
    assert res.killed_reason == KilledReason.ERROR


# --- Phase-5 UNIT 0: zero-leak proof — container count before==after on timeout --
def _count_acp_sandbox_containers() -> int:
    """Count running containers whose name starts with 'acp-sandbox-'.

    This is the oracle for the container-leak fix: after a timeout-killed run,
    the named container must be gone (stopped AND removed), not leaked.
    """
    try:
        r = subprocess.run(
            ["docker", "ps", "--filter", "name=acp-sandbox-", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        names = [n.strip() for n in r.stdout.splitlines() if n.strip()]
        return len(names)
    except Exception:
        return -1


@requires_sandbox
def test_timeout_leaves_zero_leaked_containers(snapshot: Path) -> None:
    """UNIT 0 oracle: wall-clock timeout must leave no leaked containers.

    Mechanism: the runner now gives each run a unique --name and calls
    _force_remove_container (docker stop + docker rm) on the timeout path.
    We count acp-sandbox-* containers before the run, trigger a timeout with
    an infinite loop, and assert count_after == count_before (zero delta).
    """
    before = _count_acp_sandbox_containers()
    assert before >= 0, "could not query docker ps"

    # Tight deadline so the infinite loop fires the timeout path quickly.
    runner = _runner(wall_clock_seconds=3)
    res = runner.verify_snapshot(_req(fixtures.infinite_loop_patch()), snapshot)

    assert res.killed_reason == KilledReason.DEADLINE, (
        "expected a DEADLINE kill; the timeout path must have fired"
    )

    # Brief pause for docker rm to complete (it's best-effort async subprocess).
    import time
    time.sleep(1)

    after = _count_acp_sandbox_containers()
    assert after == before, (
        f"container leak detected: {after - before} acp-sandbox-* container(s) "
        f"still running after a timeout (before={before}, after={after}). "
        "The _force_remove_container path did not clean up the named container."
    )
