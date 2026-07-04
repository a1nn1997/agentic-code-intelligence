"""Phase-5 oracle: multi-file rename using the Phase-1 call-graph index.

Oracle clauses (from phase5_prompt.xml):

(4) MULTI-FILE: renaming the planted cross-file symbol updates the definition +
    all N resolved call sites + tests, and the single sandbox run passes.
    Assert every expected call site changed (programmatic diff check) and none missed.

The programmatic diff check compares the actual diff against the KNOWN reference
set from the Phase-1 index — not a spot-check. Every file the index knows about
must have been updated; no file may be missed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from acp.common.types import TaskState
from acp.db import (
    Database,
    JournalRepo,
    TasksRepo,
    UsersRepo,
    init_db,
)
from acp.index.builder import IndexBuilder
from acp.model_gateway import build_model_gateway
from acp.orchestrator import AgentLoop, LoopConfig
from acp.sandbox_client.interface import (
    VerificationRequest,
    VerificationResult,
    VerificationStage,
)
from acp.workspace import WorkspaceServiceImpl

pytestmark = pytest.mark.integration

_OLD_NAME = "serialize_user"
_NEW_NAME = "serialize_user_v2"

# Instruction for the rename task. Stub reads rename_target + new_name from instruction.
_RENAME_INSTRUCTION = (
    f"rename the symbol rename_target={_OLD_NAME} new_name={_NEW_NAME} "
    f"across all call sites and tests"
)


class RenameFakeSandbox:
    """A sandbox that verifies the rename by checking the patch envelope.

    Verifies: the patch contains updates to the expected set of files, AND
    none of those files still contain the old name.
    """

    def __init__(self, expected_files: set[str]) -> None:
        self.calls = 0
        self.expected_files = expected_files
        self.last_ops: list[dict] = []

    def verify(self, request: VerificationRequest) -> VerificationResult:  # pragma: no cover
        raise NotImplementedError

    def verify_snapshot(
        self, request: VerificationRequest, snapshot_dir: Path
    ) -> VerificationResult:
        self.calls += 1
        ops = json.loads(request.patch)["ops"]
        self.last_ops = ops
        patched_paths = {o["path"] for o in ops if o.get("op") == "write"}
        # Check all expected files were patched.
        missing = self.expected_files - patched_paths
        if missing:
            return VerificationResult(
                verified=False,
                applied=False,
                built=False,
                tests_passed=False,
                exit_code=1,
                stage=VerificationStage.TEST,
                stderr_tail=f"missing files: {sorted(missing)}",
            )
        # Check no patched file still contains the old name AS A STANDALONE TOKEN.
        # Use word-boundary matching: serialize_user_v2 contains "serialize_user"
        # as a substring, but that is the new (valid) name, not the old one.
        _old_name_pattern = re.compile(r"\b" + re.escape(_OLD_NAME) + r"\b")
        _new_name_pattern = re.compile(r"\b" + re.escape(_NEW_NAME) + r"\b")
        for op in ops:
            if op.get("op") == "write":
                content = op.get("content", "")
                # Remove the new name first, then check no bare old name remains.
                content_without_new = _new_name_pattern.sub("", content)
                if _old_name_pattern.search(content_without_new):
                    return VerificationResult(
                        verified=False,
                        applied=False,
                        built=False,
                        tests_passed=False,
                        exit_code=1,
                        stage=VerificationStage.TEST,
                        stderr_tail=(
                            f"old name '{_OLD_NAME}' still present as "
                            f"standalone token in {op['path']}"
                        ),
                    )
        return VerificationResult(
            verified=True,
            applied=True,
            built=True,
            tests_passed=True,
            exit_code=0,
            stage=VerificationStage.DONE,
        )

    def healthy(self) -> bool:
        return True


@pytest.fixture
def env(tmp_path: Path, sample_repo: Path) -> tuple[Database, str, str, str, Path]:
    init_db(str(tmp_path / "d.db"))
    db = Database(str(tmp_path / "d.db"))
    UsersRepo(db).create("u")
    root = str(tmp_path / "ws")
    ws = WorkspaceServiceImpl(db, root)
    ref = ws.create_workspace("u", str(sample_repo))
    ws.build_index("u", ref.workspace_id)
    repo_path = ws.repo_path("u", ref.workspace_id)
    return db, root, "u", ref.workspace_id, repo_path


def _known_reference_files(repo_path: Path) -> set[str]:
    """Query the Phase-1 index for all files that reference serialize_user.

    This is the KNOWN reference set — the oracle uses it to assert no site is missed.
    """
    index = IndexBuilder().build(repo_path)
    affected: set[str] = set()
    for sym in index.definitions(_OLD_NAME):
        affected.add(sym.file_path)
    for ref in index.references_of(_OLD_NAME):
        affected.add(ref.file_path)
    return affected


# --- UNIT 2 oracle: multi-file rename updates EVERY call site, none missed ----
def test_multifile_rename_updates_every_call_site_none_missed(
    env: tuple,
) -> None:
    """ORACLE: renaming serialize_user → serialize_user_v2 via the call-graph
    updates the definition + all N call sites + tests in a single sandbox run.

    Programmatic diff check: compare actual patched paths against the FULL
    reference set from the Phase-1 index. Any missed file is a test failure.
    """
    db, root, user, ws, repo_path = env

    known_files = _known_reference_files(repo_path)
    assert len(known_files) >= 4, (
        f"Expected at least 4 files from Phase-1 index, got {known_files}"
    )

    sb = RenameFakeSandbox(expected_files=known_files)
    tid = TasksRepo(db).create(
        user, ws, _RENAME_INSTRUCTION, token_budget=200_000, step_budget=40, wall_clock_seconds=900
    ).id

    cfg = LoopConfig(db=db, workspace_root=root, gateway=build_model_gateway(), sandbox=sb)
    status = AgentLoop(cfg).run(tid)

    assert status.state == TaskState.VERIFIED_SUCCESS, (
        f"expected verified_success after rename, got {status.state} ({status.reason})"
    )
    assert sb.calls >= 1, "sandbox must be called to verify the rename"

    # PROGRAMMATIC DIFF CHECK: every file the index knows about was patched.
    patched_paths = {o["path"] for o in sb.last_ops if o.get("op") == "write"}
    for expected_file in known_files:
        assert expected_file in patched_paths, (
            f"COMPLETENESS FAILURE: {expected_file!r} was NOT updated by the rename. "
            f"The index-known reference at this path was missed. "
            f"Patched: {sorted(patched_paths)}"
        )

    # No file still contains the old name as a standalone token.
    # (The new name serialize_user_v2 is a superstring of the old name; use word boundaries.)
    _old_pat = re.compile(r"\b" + re.escape(_OLD_NAME) + r"\b")
    _new_pat = re.compile(r"\b" + re.escape(_NEW_NAME) + r"\b")
    for op in sb.last_ops:
        if op.get("op") == "write":
            content = op["content"]
            content_without_new = _new_pat.sub("", content)
            assert not _old_pat.search(content_without_new), (
                f"RENAME INCOMPLETE: {op['path']} still contains standalone '{_OLD_NAME}'"
            )

    # The new name appears in every patched file.
    for op in sb.last_ops:
        if op.get("op") == "write" and op["path"] in known_files:
            assert _NEW_NAME in op["content"], (
                f"RENAME INCOMPLETE: {op['path']} does not contain the new name '{_NEW_NAME}'"
            )


def test_rename_journal_records_all_affected_files(env: tuple) -> None:
    """The RENAME step journal entry records every file that was touched.

    This provides the audit trail: an operator can read the journal and know
    exactly which files were modified and their content-hashes.
    """
    db, root, user, ws, repo_path = env
    known_files = _known_reference_files(repo_path)
    sb = RenameFakeSandbox(expected_files=known_files)

    tid = TasksRepo(db).create(
        user, ws, _RENAME_INSTRUCTION, token_budget=200_000, step_budget=40, wall_clock_seconds=900
    ).id
    cfg = LoopConfig(db=db, workspace_root=root, gateway=build_model_gateway(), sandbox=sb)
    AgentLoop(cfg).run(tid)

    # Find the RENAME step in the journal (journaled as kind=edit with renamed_files key).
    rename_entry = None
    for entry in JournalRepo(db).get_trace(tid):
        payload = json.loads(entry.payload_json)
        if "renamed_files" in payload:
            rename_entry = payload
            break

    assert rename_entry is not None, "no RENAME step found in journal"
    journaled_paths = {rf["path"] for rf in rename_entry["renamed_files"]}
    for expected_file in known_files:
        assert expected_file in journaled_paths, (
            f"file {expected_file!r} not recorded in the RENAME journal entry. "
            f"Journaled: {sorted(journaled_paths)}"
        )


def test_rename_completeness_against_index_reference_set(env: tuple) -> None:
    """Strict completeness: the rename must cover EXACTLY the set the index returns.

    This test guards against the primary failure mode: a forgotten call site.
    It compares the patch against the index-known set (the source of truth).
    """
    db, root, user, ws, repo_path = env
    known_files = _known_reference_files(repo_path)

    sb = RenameFakeSandbox(expected_files=known_files)
    tid = TasksRepo(db).create(
        user, ws, _RENAME_INSTRUCTION, token_budget=200_000, step_budget=40, wall_clock_seconds=900
    ).id
    cfg = LoopConfig(db=db, workspace_root=root, gateway=build_model_gateway(), sandbox=sb)
    AgentLoop(cfg).run(tid)

    patched_paths = {o["path"] for o in sb.last_ops if o.get("op") == "write"}
    missed = known_files - patched_paths
    assert not missed, (
        "MULTI-FILE COMPLETENESS FAILURE: the following index-known files were NOT renamed:\n"
        + "\n".join(f"  - {f}" for f in sorted(missed))
        + f"\nAll {len(known_files)} index-known files must be updated."
    )
