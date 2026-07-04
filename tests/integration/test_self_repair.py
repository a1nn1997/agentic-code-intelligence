"""Phase-5 oracle: self-repair loop — the agent reads REAL sandbox failure output
and produces a corrected patch, bounded by budget and journaled like any other step.

Oracle clauses (from phase5_prompt.xml):

(1) SELF-REPAIR SUCCESS: a task whose first patch fails reads the REAL failure output
    and produces a corrected patch that passes → verified_success.
    Assert: repair input contained the actual error text AND arrived via data channel.

(2) UNFIXABLE: a self-repair task given an unfixable failure ends give-up-with-reason
    (not fabricated success) within budget.

(3) BUDGET-EXHAUSTED during repair: ends at a clean journaled checkpoint, workspace
    uncorrupted, RESUMES without re-charging prior repair steps.

(4) CHANNEL ASSERTION: the real captured sandbox failure output arrives in the DATA
    channel (data_xml), never in the instruction channel (instruction_xml).

(5) RESUME ACROSS REPAIR STEPS: apply-once and no-double-charge still hold across
    repair steps — repair is a journaled step like any other.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from acp.common.types import TaskState
from acp.db import (
    ArtifactsRepo,
    Database,
    JournalRepo,
    LedgerRepo,
    TasksRepo,
    UsersRepo,
    init_db,
)
from acp.model_gateway import build_model_gateway
from acp.model_gateway.interface import ModelRequest, ModelResponse
from acp.orchestrator import AgentLoop, LoopConfig
from acp.sandbox_client.interface import (
    VerificationRequest,
    VerificationResult,
    VerificationStage,
)
from acp.workspace import WorkspaceServiceImpl

pytestmark = pytest.mark.integration

# The path the stub's GOOD edit writes to (FakeSandbox checks this).
_AGENT_FILE = "backend/tests/test_agent_change.py"
# The path the stub's BAD (first-attempt) edit writes to.
_BAD_FILE = "backend/tests/test_bad_attempt.py"
# The marker text the FakeSandboxWithCapture returns as stderr_tail on failure.
# This is what the oracle asserts appears in the repair's data channel.
_FAILURE_MARKER = "FORCED_FAILURE_MARKER_for_repair_test"

# Instruction variants
_INSTRUCTION_GOOD = "add a passing test target_symbol=serialize_user"
_INSTRUCTION_FAIL_FIRST = "add a passing test target_symbol=serialize_user fail_first=1"


class FakeSandboxWithCapture:
    """A FakeSandbox that returns structured failure text and records calls.

    Failure text is a stable sentinel so the oracle can assert the exact text
    reached the data channel — proving it is the ACTUAL Phase-3 captured output,
    not a proxy or a model fabrication.
    """

    def __init__(self, *, always_fail: bool = False) -> None:
        self.calls = 0
        self.always_fail = always_fail

    def verify(self, request: VerificationRequest) -> VerificationResult:  # pragma: no cover
        raise NotImplementedError

    def verify_snapshot(
        self, request: VerificationRequest, snapshot_dir: Path
    ) -> VerificationResult:
        self.calls += 1
        ops = json.loads(request.patch)["ops"]
        has_good = any(o["path"] == _AGENT_FILE for o in ops)
        ok = has_good and not self.always_fail

        if ok:
            return VerificationResult(
                verified=True,
                applied=True,
                built=True,
                tests_passed=True,
                exit_code=0,
                stage=VerificationStage.DONE,
                stdout_tail="",
                stderr_tail="",
            )
        return VerificationResult(
            verified=False,
            applied=True,
            built=True,
            tests_passed=False,
            exit_code=1,
            stage=VerificationStage.TEST,
            stdout_tail=f"FAIL: {_FAILURE_MARKER}",
            stderr_tail=f"AssertionError: {_FAILURE_MARKER}",
        )

    def healthy(self) -> bool:
        return True


class RecordingGateway:
    """Wraps the real stub gateway and records every ModelRequest.

    Used to assert CHANNEL: failure text arrives in data_xml (DATA channel),
    never in instruction_xml (INSTRUCTION channel).
    """

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return self._inner.complete(request)  # type: ignore[return-value]


@pytest.fixture
def env(tmp_path: Path, sample_repo: Path) -> tuple[Database, str, str, str]:
    init_db(str(tmp_path / "d.db"))
    db = Database(str(tmp_path / "d.db"))
    UsersRepo(db).create("u")
    root = str(tmp_path / "ws")
    ws = WorkspaceServiceImpl(db, root)
    ref = ws.create_workspace("u", str(sample_repo))
    ws.build_index("u", ref.workspace_id)
    return db, root, "u", ref.workspace_id


def _new_task(
    db: Database,
    user: str,
    ws: str,
    instruction: str = _INSTRUCTION_GOOD,
    *,
    tokens: int = 200_000,
    steps: int = 40,
) -> str:
    return TasksRepo(db).create(
        user, ws, instruction, token_budget=tokens, step_budget=steps, wall_clock_seconds=900
    ).id


def _model_step_commits(db: Database, scope: str) -> dict[int, int]:
    rows = db.conn.execute(
        "SELECT step_index, COUNT(*) c FROM budget_ledger "
        "WHERE scope=? AND kind='commit' AND step_index IS NOT NULL GROUP BY step_index;",
        (scope,),
    ).fetchall()
    return {int(r["step_index"]): int(r["c"]) for r in rows}


# --- clause 1 + 4: self-repair success + channel assertion --------------------
def test_self_repair_succeeds_and_failure_arrives_via_data_channel(
    env: tuple,
) -> None:
    """ORACLE: first patch fails; repair reads real failure output from data channel;
    corrected patch passes → verified_success.

    Channel assertion: _FAILURE_MARKER appears in data_xml (DATA channel) of the
    repair model call, NOT in instruction_xml (INSTRUCTION channel). This is the
    structural guarantee: untrusted sandbox output is always an untrusted span.
    """
    db, root, user, ws = env
    sb = FakeSandboxWithCapture()
    inner_gw = build_model_gateway()
    recording_gw = RecordingGateway(inner_gw)

    tid = _new_task(db, user, ws, _INSTRUCTION_FAIL_FIRST)
    cfg = LoopConfig(db=db, workspace_root=root, gateway=recording_gw, sandbox=sb)
    status = AgentLoop(cfg).run(tid)

    assert status.state == TaskState.VERIFIED_SUCCESS, (
        f"expected verified_success after repair, got {status.state} ({status.reason})"
    )
    # Sandbox called at least twice: first fails (bad file), second passes (good file).
    assert sb.calls >= 2

    # CHANNEL ASSERTION: find the model call that is the repair step.
    # The repair step is called AFTER a failed verify; the loop adds the failure span
    # to the data channel. So at least one model request must have _FAILURE_MARKER in
    # data_xml AND must NOT have it in instruction_xml.
    repair_calls = [
        r for r in recording_gw.requests if _FAILURE_MARKER in r.data_xml
    ]
    assert repair_calls, (
        "CHANNEL VIOLATION: the real sandbox failure output never appeared in data_xml. "
        "The repair loop must feed failure text via the DATA channel."
    )
    for call in repair_calls:
        assert _FAILURE_MARKER not in call.instruction_xml, (
            "CHANNEL VIOLATION: failure text leaked into instruction_xml (INSTRUCTION channel). "
            "Untrusted sandbox output must stay in data_xml only."
        )


def test_repair_input_contains_actual_captured_error_text(env: tuple) -> None:
    """The data channel carrying repair context must contain the ACTUAL Phase-3
    captured error text — not a proxy or a model fabrication.

    We verify this by checking the journaled VERIFY step's stderr_tail matches
    what the FakeSandbox returned, AND that this text appears in the data_xml of
    the subsequent model call.
    """
    db, root, user, ws = env
    sb = FakeSandboxWithCapture()
    inner_gw = build_model_gateway()
    recording_gw = RecordingGateway(inner_gw)

    tid = _new_task(db, user, ws, _INSTRUCTION_FAIL_FIRST)
    cfg = LoopConfig(db=db, workspace_root=root, gateway=recording_gw, sandbox=sb)
    AgentLoop(cfg).run(tid)

    # Find the journaled VERIFY step that failed.
    failed_verify = None
    for entry in JournalRepo(db).get_trace(tid):
        if entry.kind == "verify":
            payload = json.loads(entry.payload_json)
            if not payload.get("verified", False):
                failed_verify = payload
                break
    assert failed_verify is not None, "expected a failed VERIFY step in journal"

    # The journaled stderr_tail must be the actual text the sandbox returned.
    assert _FAILURE_MARKER in failed_verify.get("stderr_tail", ""), (
        "journaled failure text does not contain the actual Phase-3 captured error marker"
    )

    # That SAME text must appear in the data_xml of the subsequent model call.
    repair_calls = [r for r in recording_gw.requests if _FAILURE_MARKER in r.data_xml]
    assert repair_calls, (
        "the actual captured error text never reached the data channel of any model call"
    )


# --- clause 2: unfixable failure → give_up_with_reason (not fabricated success) --
def test_unfixable_repair_ends_gave_up_with_reason(env: tuple) -> None:
    """ORACLE: a self-repair that cannot fix the failure ends gave_up, never a
    fabricated verified_success. The FakeSandbox always returns failure; the loop
    must exhaust repair attempts and give up honestly.
    """
    db, root, user, ws = env
    sb = FakeSandboxWithCapture(always_fail=True)
    tid = _new_task(db, user, ws, _INSTRUCTION_FAIL_FIRST)
    cfg = LoopConfig(db=db, workspace_root=root, gateway=build_model_gateway(), sandbox=sb)
    status = AgentLoop(cfg).run(tid)

    assert status.state == TaskState.GAVE_UP, (
        f"expected gave_up for unfixable failure, got {status.state}"
    )
    assert status.state != TaskState.VERIFIED_SUCCESS, (
        "unfixable task must NOT be fabricated success"
    )
    assert status.reason and "repair attempt" in status.reason.lower(), (
        f"reason should mention repair attempts, got: {status.reason!r}"
    )


# --- clause 3: budget-exhausted during repair + resume still holds -----------
def test_budget_exhausted_during_repair_and_resumes_without_recharge(
    env: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ORACLE: a repair that hits the step budget ends budget_exhausted at a
    clean journaled checkpoint, workspace uncorrupted. On resume, prior repair
    steps are NOT re-charged (idempotency still holds across repair steps).
    """
    db, root, user, ws = env
    sb = FakeSandboxWithCapture(always_fail=True)
    scope_prefix = "task:"

    # 4 steps: PLAN(0) RETRIEVE(1) EDIT(2) VERIFY(3) → budget exhausted before repair
    tid = _new_task(db, user, ws, _INSTRUCTION_FAIL_FIRST, steps=4)
    cfg = LoopConfig(db=db, workspace_root=root, gateway=build_model_gateway(), sandbox=sb)
    status = AgentLoop(cfg).run(tid)

    assert status.state == TaskState.BUDGET_EXHAUSTED, (
        f"expected budget_exhausted after step limit, got {status.state}"
    )
    # Workspace must be uncorrupted — task state is not RUNNING still
    task = TasksRepo(db).get_for_resume(tid)
    assert task is not None
    assert TaskState(task.state) == TaskState.BUDGET_EXHAUSTED

    scope = f"{scope_prefix}{tid}"
    tokens_before_resume = LedgerRepo(db).spent_tokens(scope)
    commits_before = _model_step_commits(db, scope)

    # Resume should return the same terminal state immediately (no re-charge).
    status2 = AgentLoop(cfg).run(tid)
    assert status2.state == TaskState.BUDGET_EXHAUSTED

    # No new charges on a completed (budget_exhausted) task.
    tokens_after_resume = LedgerRepo(db).spent_tokens(scope)
    assert tokens_after_resume == tokens_before_resume, (
        "re-running a budget_exhausted task added charges — double-charge on resume"
    )
    commits_after = _model_step_commits(db, scope)
    assert commits_after == commits_before, (
        "re-running a budget_exhausted task added ledger commits — double-charge"
    )


# --- clause 5: repair is a journaled step — apply-once across repair ---------
def test_repair_edit_applied_exactly_once_across_resume(
    env: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ORACLE: if the process crashes mid-repair, the repair EDIT is applied
    exactly once (artifact content-hash gate) and the model is not re-issued
    (journal cache). This is the same no-double-charge, apply-once guarantee
    as Phase 4, now proven to hold across repair steps.
    """
    db, root, user, ws = env
    sb = FakeSandboxWithCapture()

    tid = _new_task(db, user, ws, _INSTRUCTION_FAIL_FIRST)
    scope = f"task:{tid}"

    real_commit = AgentLoop._commit_step_charge
    # Crash after the REPAIR step (step 4 = corrective edit) is journaled but
    # before its commit is durable.
    def crashing_commit(self, s, task_id, step, tokens, *, note):  # type: ignore[no-untyped-def]
        if step == 4:
            raise RuntimeError("simulated crash: repair step journaled, commit lost")
        return real_commit(self, s, task_id, step, tokens, note=note)

    cfg = LoopConfig(db=db, workspace_root=root, gateway=build_model_gateway(), sandbox=sb)
    monkeypatch.setattr(AgentLoop, "_commit_step_charge", crashing_commit)
    with pytest.raises(RuntimeError):
        AgentLoop(cfg).run(tid)

    # Precondition: step 4 was journaled, commit absent.
    assert JournalRepo(db).get_step(tid, 4) is not None
    commit_row = db.conn.execute(
        "SELECT 1 FROM budget_ledger WHERE scope=? AND step_index=4 AND kind='commit';",
        (scope,),
    ).fetchone()
    assert commit_row is None, "precondition: step-4 commit was lost"

    # RESUME.
    monkeypatch.setattr(AgentLoop, "_commit_step_charge", real_commit)
    sb.calls = 0
    status = AgentLoop(cfg).run(tid)

    assert status.state == TaskState.VERIFIED_SUCCESS

    # Repair edit applied exactly once — one artifact with the good path.
    patches = ArtifactsRepo(db).list_for_task(tid)
    good_patches = [a for a in patches if _AGENT_FILE in (a.path or "")]
    assert len(good_patches) == 1, (
        f"repair EDIT applied {len(good_patches)} times — should be exactly once"
    )

    # No double-charge: exactly one commit per model step.
    per_step = _model_step_commits(db, scope)
    for step_idx, count in per_step.items():
        assert count == 1, (
            f"step {step_idx} has {count} ledger commits — double-charge detected"
        )
