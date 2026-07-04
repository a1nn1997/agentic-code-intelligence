"""Unit tests for the Phase-8 polyglot runner host-side logic — no Docker required.

Tests cover:
  - Config-selection routing (SANDBOX_RUNNER -> correct runner class)
  - Each runner inherits the full parent isolation logic (docker args check)
  - Each runner uses its distinct image name by default
  - The container-leak fix (unique --name) is present in all runners
  - Each runner conforms to the SandboxClient Protocol

These tests exercise what CAN be tested without Docker. The full Phase-3 oracle
clauses against real Docker are in test_polyglot_parity.py (marked docker).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from acp.sandbox_client import build_sandbox_client
from acp.sandbox_client.docker_runner import DockerSandboxRunner, SandboxLimits
from acp.sandbox_client.interface import SandboxClient
from acp.sandbox_client.polyglot_runners import (
    GoSandboxRunner,
    RustSandboxRunner,
    TsSandboxRunner,
)

pytestmark = pytest.mark.unit


# --- config-selection routing ------------------------------------------------

def test_python_runner_is_default() -> None:
    from acp.config.settings import SandboxRunner, Settings
    s = Settings(sandbox_runner=SandboxRunner.PYTHON, database_url="sqlite:///./var/acp.db")
    client = build_sandbox_client(s)
    assert type(client) is DockerSandboxRunner  # exact type, not a subclass


def test_go_runner_routed_by_config() -> None:
    from acp.config.settings import SandboxRunner, Settings
    s = Settings(sandbox_runner=SandboxRunner.GO, database_url="sqlite:///./var/acp.db")
    client = build_sandbox_client(s)
    assert isinstance(client, GoSandboxRunner)


def test_rust_runner_routed_by_config() -> None:
    from acp.config.settings import SandboxRunner, Settings
    s = Settings(sandbox_runner=SandboxRunner.RUST, database_url="sqlite:///./var/acp.db")
    client = build_sandbox_client(s)
    assert isinstance(client, RustSandboxRunner)


def test_ts_runner_routed_by_config() -> None:
    from acp.config.settings import SandboxRunner, Settings
    s = Settings(sandbox_runner=SandboxRunner.TS, database_url="sqlite:///./var/acp.db")
    client = build_sandbox_client(s)
    assert isinstance(client, TsSandboxRunner)


# --- each runner is a SandboxClient ------------------------------------------

@pytest.mark.parametrize("cls,image", [
    (GoSandboxRunner, "acp-sandbox-go:latest"),
    (RustSandboxRunner, "acp-sandbox-rust:latest"),
    (TsSandboxRunner, "acp-sandbox-ts:latest"),
])
def test_runner_conforms_to_sandboxclient_protocol(cls: type, image: str) -> None:
    runner = cls(image=image)
    # runtime_checkable Protocol check
    assert isinstance(runner, SandboxClient)


# --- each runner uses its distinct default image -----------------------------

def test_go_runner_default_image() -> None:
    runner = GoSandboxRunner()
    assert runner._image == "acp-sandbox-go:latest"


def test_rust_runner_default_image() -> None:
    runner = RustSandboxRunner()
    assert runner._image == "acp-sandbox-rust:latest"


def test_ts_runner_default_image() -> None:
    runner = TsSandboxRunner()
    assert runner._image == "acp-sandbox-ts:latest"


# --- isolation flags present in each runner's docker invocation --------------
# Parity by construction: all runners inherit _docker_args from DockerSandboxRunner.
# Verify the flags are present for each runner's distinct image.

@pytest.mark.parametrize("cls,image", [
    (GoSandboxRunner, "acp-sandbox-go:latest"),
    (RustSandboxRunner, "acp-sandbox-rust:latest"),
    (TsSandboxRunner, "acp-sandbox-ts:latest"),
])
def test_runner_docker_args_carry_isolation_flags(
    cls: type, image: str, tmp_path: Path
) -> None:
    snap = tmp_path / "snap"
    patch = tmp_path / "patch"
    snap.mkdir()
    patch.mkdir()
    runner = cls(
        image=image,
        limits=SandboxLimits(cpus=1.0, memory_mb=256, tmpfs_mb=128, pids=64),
    )
    test_name = "acp-sandbox-test-abc123"
    args = runner._docker_args(snap, patch, test_name)
    joined = " ".join(args)

    assert "--network none" in joined
    assert "--memory 256m" in joined
    assert "--memory-swap 256m" in joined  # swap disabled
    assert "--read-only" in args
    assert "--cap-drop ALL" in joined
    assert "no-new-privileges" in joined
    assert "--user 10001:10001" in joined
    # Phase-5 leak fix: unique --name present
    assert "--name" in args
    assert test_name in args
    # Correct image for this runner
    assert image in args


# --- each runner inherits the Phase-5 container-leak fix ---------------------
# The fix lives in the parent DockerSandboxRunner. Verify via the docker args
# that --name is present (unique per-run name is the mechanism of the fix).

@pytest.mark.parametrize("cls", [GoSandboxRunner, RustSandboxRunner, TsSandboxRunner])
def test_runner_uses_unique_name_for_leak_fix(cls: type, tmp_path: Path) -> None:
    snap = tmp_path / "snap"
    patch = tmp_path / "patch"
    snap.mkdir()
    patch.mkdir()
    runner = cls()
    # Generate two sets of args (different run names) and check --name is present.
    args1 = runner._docker_args(snap, patch, "acp-sandbox-aaa111")
    args2 = runner._docker_args(snap, patch, "acp-sandbox-bbb222")
    assert "--name" in args1
    assert "acp-sandbox-aaa111" in args1
    assert "--name" in args2
    assert "acp-sandbox-bbb222" in args2
    # Different names for different runs.
    assert args1 != args2


# --- runner limits are threaded through from settings ------------------------

def test_limits_from_settings_reach_go_runner() -> None:
    from acp.config.settings import SandboxRunner, Settings
    s = Settings(
        sandbox_runner=SandboxRunner.GO,
        sandbox_memory_mb=768,
        sandbox_cpus=2.0,
        database_url="sqlite:///./var/acp.db",
    )
    client = build_sandbox_client(s)
    assert isinstance(client, GoSandboxRunner)
    assert client._limits.memory_mb == 768
    assert client._limits.cpus == 2.0
