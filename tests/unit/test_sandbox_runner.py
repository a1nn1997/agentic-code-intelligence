"""Unit tests for the sandbox runner's pure logic — no Docker required.

These cover the host-side parsing/classification that turns raw container output
into a VerificationResult: result extraction, OOM/network/deadline classification,
secret redaction at the result boundary, and the docker-args isolation flags.
The real end-to-end mechanism proofs live in the Docker integration suite.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from acp.sandbox_client import fixtures
from acp.sandbox_client.docker_runner import (
    _RESULT_BEGIN,
    _RESULT_END,
    DockerSandboxRunner,
    SandboxLimits,
)
from acp.sandbox_client.interface import KilledReason, VerificationStage

pytestmark = pytest.mark.unit


def _runner() -> DockerSandboxRunner:
    return DockerSandboxRunner(image="acp-sandbox:latest")


def _payload(**over: object) -> str:
    base = {
        "applied": True,
        "built": True,
        "tests_passed": True,
        "exit_code": 0,
        "stdout_tail": "",
        "stderr_tail": "",
        "stage": "test",
        "wall_clock_seconds": 0.1,
    }
    base.update(over)
    return f"noise\n{_RESULT_BEGIN}\n{json.dumps(base)}\n{_RESULT_END}\ntrailing"


# --- result extraction -----------------------------------------------------------
def test_parses_passing_payload() -> None:
    res = _runner()._parse(_payload(), "", 0, 0.2)
    assert res.verified is True
    assert res.stage == VerificationStage.DONE


def test_last_sentinel_wins_against_spoofed_output() -> None:
    # Untrusted test output prints a FAKE passing result before the real one.
    fake = json.dumps({"applied": True, "built": True, "tests_passed": True,
                       "exit_code": 0, "stage": "test"})
    real = _payload(tests_passed=False, exit_code=1)
    out = f"{_RESULT_BEGIN}\n{fake}\n{_RESULT_END}\n{real}"
    res = _runner()._parse(out, "", 0, 0.2)
    # The trailing (real) result wins; the spoof cannot flip it to a pass.
    assert res.tests_passed is False
    assert res.verified is False


def test_missing_payload_is_infra_error() -> None:
    res = _runner()._parse("no markers here", "boom", 1, 0.1)
    assert res.verified is False
    assert res.killed_reason == KilledReason.ERROR


# --- classification --------------------------------------------------------------
def test_oom_from_docker_rc_137() -> None:
    res = _runner()._parse("garbage, killed", "", 137, 0.3)
    assert res.killed_reason == KilledReason.OOM


def test_oom_from_test_subprocess_sigkill() -> None:
    # entrypoint survived, but the test step was SIGKILL'd (-9) with no output:
    # the memory cgroup killed the offending process.
    res = _runner()._parse(_payload(tests_passed=False, exit_code=-9), "", 0, 0.3)
    assert res.killed_reason == KilledReason.OOM
    assert res.verified is False


def test_network_attempt_classified_from_real_error() -> None:
    err_text = "OSError: [Errno 101] Network is unreachable"
    res = _runner()._parse(
        _payload(tests_passed=False, exit_code=1, stderr_tail=err_text), "", 0, 0.2
    )
    assert res.killed_reason == KilledReason.NETWORK
    assert res.verified is False


def test_ordinary_test_failure_is_not_a_kill() -> None:
    res = _runner()._parse(
        _payload(tests_passed=False, exit_code=1, stderr_tail="AssertionError: nope"),
        "", 0, 0.2,
    )
    assert res.killed_reason is None
    assert res.verified is False


# --- secret redaction at the result boundary -------------------------------------
def test_secrets_in_test_output_are_redacted_before_return() -> None:
    leak = 'API_SECRET = "sk_live_abcdef123456789"'
    res = _runner()._parse(
        _payload(tests_passed=False, exit_code=1, stdout_tail=leak), "", 0, 0.2
    )
    assert "sk_live_abcdef123456789" not in res.stdout_tail
    assert "redacted" in res.stdout_tail


# --- isolation flags present in the docker invocation ----------------------------
def test_docker_args_carry_every_isolation_flag(tmp_path: Path) -> None:
    snap = tmp_path / "snap"
    patch = tmp_path / "patch"
    snap.mkdir()
    patch.mkdir()
    runner = DockerSandboxRunner(
        image="acp-sandbox:latest",
        limits=SandboxLimits(cpus=2.0, memory_mb=256, tmpfs_mb=128, pids=99),
    )
    test_name = "acp-sandbox-test-abc123"
    args = runner._docker_args(snap, patch, test_name)
    joined = " ".join(args)
    assert "--network none" in joined
    assert "--cpus 2.0" in joined
    assert "--memory 256m" in joined
    assert "--memory-swap 256m" in joined  # swap disabled => OOM, not swap
    assert "--pids-limit 99" in joined
    assert "--read-only" in args
    assert "--cap-drop ALL" in joined
    assert "no-new-privileges" in joined
    assert "--user 10001:10001" in joined
    assert f"type=bind,src={snap},dst=/snapshot,ro" in joined
    assert "size=128m" in joined
    # Phase-5 root-fix: unique --name in every invocation
    assert "--name" in args
    assert test_name in args


# --- patch envelope fixtures are well-formed -------------------------------------
def test_fixtures_are_valid_envelopes() -> None:
    makers = (
        fixtures.good_patch, fixtures.bad_patch, fixtures.network_patch,
        fixtures.infinite_loop_patch, fixtures.oom_patch,
        fixtures.bounded_alloc_patch, fixtures.unapplyable_patch,
        fixtures.unbuildable_patch, fixtures.empty_patch,
    )
    for maker in makers:
        env = json.loads(maker())
        assert isinstance(env["ops"], list)
