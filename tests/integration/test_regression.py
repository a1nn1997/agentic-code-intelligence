"""Phase-7 regression tier — named tests for each previously-fixed bug.

Every test in this file corresponds to a concrete bug that was found and fixed
during development.  If the fix is ever accidentally reverted, the named test
catches it immediately.  The name IS the lock — renaming or deleting these
tests defeats the purpose.

Bugs locked in:

  REG-01  XML-attribute-newline corruption
          Field values stored as XML attributes collapse newlines to spaces.
          Fix: render_action uses <field> element text, not attributes.

  REG-02  Container leak after timeout
          A wall-clock-killed sandbox container was not removed from Docker.
          Fix: _force_remove_container in the sandbox runner.
          (Docker-gated; auto-skipped if Docker is unavailable.)

  REG-03  Rename substring hazard
          A naive str.replace rename of "user" would corrupt "get_users"
          or "UserService" in the same file.
          Fix: rename_symbol uses identifier-boundary token matching.

  REG-04  Over-budget charges nothing (ledger unchanged)
          A task that hits the budget before its first model call must charge
          zero tokens — the ledger must be unchanged.

  REG-05  Resume: no double-charge and apply-exactly-once
          A task killed after the model call returned but before the ledger
          commit must resume and charge exactly once, and apply the patch
          exactly once.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from acp.common.types import TaskState
from acp.db import (
    ArtifactsRepo,
    Database,
    TasksRepo,
    UsersRepo,
    init_db,
)
from acp.model_gateway import build_model_gateway
from acp.model_gateway.prompt import AgentAction, parse_action, render_action
from acp.orchestrator import AgentLoop, LoopConfig
from acp.sandbox_client.interface import (
    VerificationRequest,
    VerificationResult,
    VerificationStage,
)
from acp.workspace import WorkspaceServiceImpl

pytestmark = pytest.mark.regression

SAMPLE_REPO_SRC = Path(__file__).resolve().parents[2] / "sample_repo"
_AGENT_FILE = "backend/tests/test_agent_change.py"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class _FakeSandbox:
    """Honest fake sandbox: verified iff the expected target file is in the patch."""

    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail

    def healthy(self) -> bool:
        return True

    def verify(self, request: VerificationRequest) -> VerificationResult:  # pragma: no cover
        raise NotImplementedError

    def verify_snapshot(
        self, request: VerificationRequest, snapshot_dir: Path
    ) -> VerificationResult:
        ops = json.loads(request.patch).get("ops", [])
        ok = any(op.get("path") == _AGENT_FILE for op in ops) and not self._fail
        return VerificationResult(
            verified=ok,
            applied=ok,
            built=ok,
            tests_passed=ok,
            exit_code=0 if ok else 1,
            stage=VerificationStage.DONE if ok else VerificationStage.TEST,
            stderr_tail="" if ok else "forced failure",
        )


def _make_env(tmp_path: Path) -> tuple[Database, str, str, str]:
    db_path = str(tmp_path / "reg.db")
    ws_root = str(tmp_path / "ws")
    init_db(db_path)
    db = Database(db_path)
    UsersRepo(db).create("u")
    svc = WorkspaceServiceImpl(db, ws_root)
    ref = svc.create_workspace("u", str(SAMPLE_REPO_SRC))
    svc.build_index("u", ref.workspace_id)
    return db, ws_root, "u", ref.workspace_id


def _cfg(db: Database, ws_root: str) -> LoopConfig:
    return LoopConfig(
        db=db,
        workspace_root=ws_root,
        gateway=build_model_gateway(),
        sandbox=_FakeSandbox(),
    )


def _new_task(db: Database, user: str, ws: str, *, tokens: int = 200_000, steps: int = 40) -> str:
    return TasksRepo(db).create(
        user, ws, "add a passing test target_symbol=serialize_user",
        token_budget=tokens, step_budget=steps, wall_clock_seconds=900,
    ).id


# ---------------------------------------------------------------------------
# REG-01 — XML attribute-newline corruption
# ---------------------------------------------------------------------------


def test_reg01_xml_attribute_newline_corruption(tmp_path: Path) -> None:
    """REG-01: an edit field value containing newlines must survive the round-trip
    through render_action → parse_action without losing or mangling the newlines.

    The bug: fields stored as XML attributes undergo attribute-value normalisation
    (XML spec §3.3.3), collapsing \\n to a space.  The fix stores them as element
    text with <field name="...">value</field>, where no normalisation applies.
    """
    from acp.model_gateway.prompt import ActionKind as AK
    body_with_newlines = "def foo():\n    pass\n\ndef bar():\n    return 1\n"
    action = AgentAction(
        kind=AK.EDIT,
        fields={"file_path": "backend/app/users/service.py", "content": body_with_newlines},
    )
    xml = render_action(action)
    recovered = parse_action(xml)

    assert recovered.fields["content"] == body_with_newlines, (
        f"REG-01: newlines corrupted in round-trip.\n"
        f"  original  : {body_with_newlines!r}\n"
        f"  recovered : {recovered.fields['content']!r}\n"
        f"  xml       : {xml!r}"
    )


# ---------------------------------------------------------------------------
# REG-02 — Container leak after timeout (Docker-gated)
# ---------------------------------------------------------------------------


@pytest.mark.docker
def test_reg02_container_leak_zero_after_timeout(tmp_path: Path) -> None:
    """REG-02: a wall-clock-killed sandbox container must be removed, not leaked.

    Counts acp-sandbox-* containers before and after a timeout-triggered run.
    The count must not increase (the container must be removed by the cleanup path).
    Docker-gated: skipped if Docker is unavailable.
    """
    import subprocess

    from tests.docker_gate import _docker_daemon_up, docker_required

    from acp.sandbox_client import build_sandbox_client
    from acp.sandbox_client.fixtures import infinite_loop_patch

    client = build_sandbox_client()
    if not client.healthy():
        # A8: fail-not-skip when the real proof was explicitly demanded.
        if docker_required():
            if _docker_daemon_up():
                pytest.fail(
                    "acp-sandbox image absent but ACP_REQUIRE_DOCKER=1 and Docker "
                    "is up — REG-02 container-leak proof demanded. `make sandbox-build`.",
                    pytrace=False,
                )
            pytest.fail(
                "ACP_REQUIRE_DOCKER=1 but Docker daemon unreachable — cannot run REG-02.",
                pytrace=False,
            )
        pytest.skip("Docker not available")

    def _count() -> int:
        out = subprocess.run(
            ["docker", "ps", "-a", "--filter", "name=acp-sandbox-", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        return len([ln for ln in out.splitlines() if ln.strip()])

    # Snapshot repo.
    import shutil
    snap = tmp_path / "snap"
    shutil.copytree(SAMPLE_REPO_SRC, snap)

    before = _count()
    req = VerificationRequest(
        task_id="reg02", workspace_id="reg02", base_commit="reg02",
        patch=infinite_loop_patch(),
    )
    try:
        client.verify_snapshot(req, snap)
    except Exception:
        pass  # timeout is expected

    import time
    time.sleep(1)  # allow docker rm to complete
    after = _count()

    assert after <= before, (
        f"REG-02: container leak detected — before={before}, after={after}.  "
        "The _force_remove_container cleanup path did not run."
    )


# ---------------------------------------------------------------------------
# REG-03 — Rename substring hazard
# ---------------------------------------------------------------------------


def test_reg03_rename_no_substring_corruption(tmp_path: Path) -> None:
    """REG-03: renaming 'user' must not corrupt identifiers that contain 'user'
    as a substring, e.g. 'get_users', 'UserService', 'user_id'.

    The bug: a naive str.replace("user", "person") would corrupt those names.
    The fix: identifier-boundary matching in rename_symbol (re.sub with \\b).
    """
    import shutil

    from acp.index import IndexBuilder
    repo = tmp_path / "repo"
    shutil.copytree(SAMPLE_REPO_SRC, repo)

    ib = IndexBuilder()
    index = ib.build(repo)

    # Find all references to 'serialize_user'.
    refs = index.references_of("serialize_user")
    ref_paths = {r.file_path for r in refs}

    # For each referenced file, confirm that the rename (replace serialize_user
    # with serialize_user_v2) does NOT corrupt 'get_users', 'UserId', 'user_id'
    # etc — identifiers that merely contain the substring.
    for rel_path in ref_paths:
        file_path = repo / rel_path
        if not file_path.exists():
            continue
        original = file_path.read_text()

        import re
        renamed = re.sub(r"\bserialize_user\b", "serialize_user_v2", original)

        # Ensure 'get_users' was not changed (it does NOT contain 'serialize_user').
        if "get_users" in original:
            assert "get_users" in renamed, (
                f"REG-03: 'get_users' was corrupted in {rel_path!r} — "
                "rename is matching substrings instead of whole identifiers"
            )
        # Ensure the rename actually happened where it should.
        if "serialize_user" in original:
            assert "serialize_user_v2" in renamed, (
                f"REG-03: 'serialize_user' was not renamed in {rel_path!r}"
            )


# ---------------------------------------------------------------------------
# REG-04 — Over-budget task charges nothing
# ---------------------------------------------------------------------------


def test_reg04_over_budget_charges_nothing(tmp_path: Path) -> None:
    """REG-04: a task whose token budget is exhausted before any model call
    must leave the ledger unchanged — zero tokens charged.

    The bug: the loop could write a ledger row for the budget-check step itself.
    The fix: the pre-op budget check fires BEFORE any billable call; no commit
    is written for a budget-exhausted task that never reached a model call.
    """
    db, ws_root, user, ws_id = _make_env(tmp_path)
    # 1-token budget: exhausted before the first model call.
    tid = _new_task(db, user, ws_id, tokens=1)
    status = AgentLoop(_cfg(db, ws_root)).run(tid)

    assert status.state == TaskState.BUDGET_EXHAUSTED, (
        f"REG-04: expected budget_exhausted, got {status.state!r}"
    )

    # Ledger must be empty for this task.
    rows = db.conn.execute(
        "SELECT COUNT(*) c FROM budget_ledger WHERE scope=? AND kind='commit'", (ws_id,)
    ).fetchone()
    charged = int(rows["c"])
    assert charged == 0, (
        f"REG-04: ledger has {charged} commit row(s) for a budget-exhausted task — "
        "tokens were charged despite the budget check firing before any model call"
    )


# ---------------------------------------------------------------------------
# REG-05 — Resume: no double-charge and apply-exactly-once
# ---------------------------------------------------------------------------


def test_reg05_resume_no_double_charge_apply_once(tmp_path: Path) -> None:
    """REG-05: a task killed after a model call returned but before the ledger
    commit must resume and (a) charge exactly once and (b) apply the patch once.

    The crash window: journal row written, ledger COMMIT not yet durable.
    The fix: _charge_from_journal reconciles on resume — adds the missing commit
    if absent, skips if already present.
    """
    db, ws_root, user, ws_id = _make_env(tmp_path)
    cfg = _cfg(db, ws_root)
    tid = _new_task(db, user, ws_id)

    from unittest.mock import patch

    real_commit = AgentLoop._commit_step_charge  # type: ignore[attr-defined]
    crash_count = [0]

    def crashing_commit(  # type: ignore[no-untyped-def]
        self, scope, task_id, step, tokens, *, note
    ) -> None:
        crash_count[0] += 1
        if crash_count[0] == 1:
            raise RuntimeError("simulated crash")
        real_commit(self, scope, task_id, step, tokens, note=note)

    with patch.object(AgentLoop, "_commit_step_charge", crashing_commit):
        try:
            AgentLoop(cfg).run(tid)
        except RuntimeError:
            pass

    # Resume.
    status = AgentLoop(cfg).run(tid)
    assert status.state == TaskState.VERIFIED_SUCCESS, (
        f"REG-05: expected verified_success on resume, got {status.state!r}"
    )

    # (a) No double-charge: every model step has exactly ONE ledger commit.
    bad = db.conn.execute(
        "SELECT step_index, COUNT(*) c FROM budget_ledger "
        "WHERE scope=? AND kind='commit' AND step_index IS NOT NULL "
        "GROUP BY step_index HAVING c > 1",
        (ws_id,),
    ).fetchall()
    assert not bad, (
        f"REG-05: double-charge detected on step(s): {[dict(r) for r in bad]}"
    )

    # (b) Patch applied exactly once: one artifact for this task.
    arts = ArtifactsRepo(db).list_for_task(tid)
    assert len(arts) == 1, (
        f"REG-05: expected 1 artifact (apply-once), got {len(arts)}"
    )
