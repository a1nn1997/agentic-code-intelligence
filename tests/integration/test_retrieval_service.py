"""Integration: the Phase-2 budgeted RetrievalService over the Phase-1 sample
repo + index, metered against the real budget_ledger. No model, no network.

Each test maps to one clause of the Phase-2 oracle:

1. span costs strictly fewer tokens/bytes than the whole file;
2. same query + same snapshot ⇒ byte-identical result AND identical accounting;
3. over-budget retrieval raises BudgetExceeded, returns NO content, and charges
   NOTHING (ledger row count AND balance unchanged);
4. read_span over the planted-secret file returns redacted content (the fake
   value never appears) and records a redaction event;
5. search_symbols / references reproduce the Phase-1 cross-file resolution;
6. every served primitive writes a metering entry to the ledger.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from acp.common.errors import BudgetExceeded
from acp.common.types import LedgerEntryKind
from acp.db import Database, UsersRepo
from acp.db.repositories import LedgerRepo
from acp.retrieval import RetrievalServiceImpl
from acp.retrieval.interface import RetrievalService, SnapshotRef, SpanRef
from acp.workspace import WorkspaceServiceImpl

pytestmark = pytest.mark.integration

_PLANTED_CONFIG_SECRET = "sk-live-1234567890abcdefADVERSARIALdeadbeef"


def _setup(
    db: Database, workspace_root: Path, sample_repo: Path, *, budget: int = 1_000_000
) -> tuple[RetrievalServiceImpl, SnapshotRef, str]:
    UsersRepo(db).create("user_a")
    ws = WorkspaceServiceImpl(db, workspace_root)
    ref = ws.create_workspace("user_a", str(sample_repo))
    ws.build_index("user_a", ref.workspace_id)
    scope = f"task:{ref.workspace_id}"
    svc = RetrievalServiceImpl(
        db, workspace_root, "user_a", scope=scope, budget_tokens=budget
    )
    assert ref.head_commit is not None
    return svc, SnapshotRef(workspace_id=ref.workspace_id, commit=ref.head_commit), scope


def _ledger_rows(db: Database, scope: str) -> int:
    row = db.conn.execute(
        "SELECT COUNT(*) AS n FROM budget_ledger WHERE scope = ?;", (scope,)
    ).fetchone()
    return int(row["n"])


def test_service_satisfies_protocol(
    db: Database, workspace_root: Path, sample_repo: Path
) -> None:
    svc, _, _ = _setup(db, workspace_root, sample_repo)
    assert isinstance(svc, RetrievalService)


# --- Oracle clause 1: span strictly cheaper than whole file -----------------
def test_span_costs_strictly_less_than_whole_file(
    db: Database, workspace_root: Path, sample_repo: Path
) -> None:
    svc, snap, _ = _setup(db, workspace_root, sample_repo)
    file = "backend/app/users/service.py"
    span = svc.read_span(snap, SpanRef(file_path=file, start_line=29, end_line=34))
    whole = svc.read_file(snap, file)
    assert span.token_cost < whole.token_cost
    assert span.byte_count < whole.byte_count


# --- Oracle clause 2: determinism (content AND accounting) ------------------
def test_same_query_same_snapshot_is_byte_identical(
    db: Database, workspace_root: Path, sample_repo: Path
) -> None:
    svc, snap, _ = _setup(db, workspace_root, sample_repo)
    span = SpanRef(file_path="backend/app/users/service.py", start_line=13, end_line=26)
    a = svc.read_span(snap, span)
    b = svc.read_span(snap, span)
    assert a.content == b.content  # byte-identical content
    assert (a.token_cost, a.byte_count) == (b.token_cost, b.byte_count)  # identical accounting


def test_metadata_primitives_are_deterministic(
    db: Database, workspace_root: Path, sample_repo: Path
) -> None:
    svc, snap, _ = _setup(db, workspace_root, sample_repo)
    assert svc.references(snap, "serialize_user") == svc.references(snap, "serialize_user")
    assert svc.search_symbols(snap, "user") == svc.search_symbols(snap, "user")


# --- Oracle clause 3: over-budget refuses and charges NOTHING ---------------
def test_over_budget_raises_and_charges_nothing(
    db: Database, workspace_root: Path, sample_repo: Path
) -> None:
    # Budget too small to afford even a small read.
    svc, snap, scope = _setup(db, workspace_root, sample_repo, budget=3)
    ledger = LedgerRepo(db)

    rows_before = _ledger_rows(db, scope)
    spent_before = ledger.spent_tokens(scope)
    reserved_before = ledger.reserved_tokens(scope)

    with pytest.raises(BudgetExceeded):
        svc.read_file(snap, "backend/app/config.py")

    # The refused call charged NOTHING: row count AND balance are UNCHANGED.
    assert _ledger_rows(db, scope) == rows_before
    assert ledger.spent_tokens(scope) == spent_before
    assert ledger.reserved_tokens(scope) == reserved_before


def test_budget_partially_spent_then_next_call_refused_leaves_ledger_intact(
    db: Database, workspace_root: Path, sample_repo: Path
) -> None:
    """A served call spends; a subsequent unaffordable call is refused and adds
    no further ledger rows (the refusal is clean even after prior spend)."""
    svc, snap, scope = _setup(db, workspace_root, sample_repo, budget=120)
    ledger = LedgerRepo(db)
    # First read fits and is charged.
    svc.read_span(snap, SpanRef(file_path="backend/app/config.py", start_line=1, end_line=3))
    rows_after_first = _ledger_rows(db, scope)
    spent_after_first = ledger.spent_tokens(scope)
    assert spent_after_first > 0

    # Now the whole file cannot fit in what remains.
    with pytest.raises(BudgetExceeded):
        svc.read_file(snap, "backend/app/config.py")
    assert _ledger_rows(db, scope) == rows_after_first
    assert ledger.spent_tokens(scope) == spent_after_first


# --- Oracle clause 4: secret redaction at the boundary ----------------------
def test_read_span_over_planted_secret_is_redacted_and_logged(
    db: Database, workspace_root: Path, sample_repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    svc, snap, _ = _setup(db, workspace_root, sample_repo)
    with caplog.at_level("INFO"):
        result = svc.read_span(
            snap, SpanRef(file_path="backend/app/config.py", start_line=10, end_line=15)
        )
    # The fake secret VALUE never appears in retrieval output.
    assert _PLANTED_CONFIG_SECRET not in result.content
    assert "«redacted" in result.content
    # A redaction event was recorded (metered), and it never logs the value.
    redaction_logs = [r for r in caplog.records if r.getMessage() == "retrieval.secret_redacted"]
    assert redaction_logs
    for record in caplog.records:
        assert _PLANTED_CONFIG_SECRET not in record.getMessage()


def test_env_secret_never_leaves_via_read(
    db: Database, workspace_root: Path, sample_repo: Path
) -> None:
    svc, snap, _ = _setup(db, workspace_root, sample_repo)
    result = svc.read_file(snap, ".env")
    for value in (
        "sk_live_PLANTEDsecretDEADBEEF0123456789abcdef",
        "hunter2-planted-do-not-use",
        "eyJfake.PLANTED.signingkey",
    ):
        assert value not in result.content


# --- Oracle clause 5: cross-file resolution matches Phase 1 -----------------
def test_references_reproduce_phase1_cross_file_resolution(
    db: Database, workspace_root: Path, sample_repo: Path
) -> None:
    svc, snap, _ = _setup(db, workspace_root, sample_repo)
    files = sorted({r.file_path for r in svc.references(snap, "serialize_user")})
    assert files == [
        "backend/app/reports/export.py",
        "backend/app/users/api.py",
        "backend/app/users/service.py",
        "backend/tests/test_users.py",
    ]
    # And the TypeScript side resolves too: formatUser is defined in
    # models/user.ts and *referenced* from its 4 caller files (the defining
    # occurrence is a Symbol, not a Reference — Phase-1 index semantics).
    ts_files = sorted({r.file_path for r in svc.references(snap, "formatUser")})
    assert ts_files == [
        "frontend/src/api/usersClient.ts",
        "frontend/src/components/userList.ts",
        "frontend/src/pages/usersPage.ts",
        "frontend/tests/users.test.ts",
    ]
    # The definition itself resolves to the models file.
    formatuser_def = svc.definition(snap, "formatUser")
    assert formatuser_def is not None
    assert formatuser_def.file_path == "frontend/src/models/user.ts"


def test_definition_resolves_defining_span(
    db: Database, workspace_root: Path, sample_repo: Path
) -> None:
    svc, snap, _ = _setup(db, workspace_root, sample_repo)
    d = svc.definition(snap, "serialize_user")
    assert d is not None
    assert d.file_path == "backend/app/users/service.py"
    assert d.kind == "function"


def test_structural_grep_ignores_docstring_injection(
    db: Database, workspace_root: Path, sample_repo: Path
) -> None:
    """The planted injection lives in a docstring; structural_grep reads the
    index (symbol names), never comment/string bytes, so it cannot surface it."""
    svc, snap, _ = _setup(db, workspace_root, sample_repo)
    hits = svc.structural_grep(snap, "ignore all previous")
    assert hits == []


# --- Oracle clause 6: every primitive writes a metering entry ---------------
def test_every_primitive_meters_the_ledger(
    db: Database, workspace_root: Path, sample_repo: Path
) -> None:
    svc, snap, scope = _setup(db, workspace_root, sample_repo)
    ledger = LedgerRepo(db)

    def committed() -> int:
        return ledger.spent_tokens(scope)

    calls = [
        lambda: svc.search_symbols(snap, "user"),
        lambda: svc.definition(snap, "serialize_user"),
        lambda: svc.references(snap, "serialize_user"),
        lambda: svc.read_span(
            snap, SpanRef(file_path="backend/app/users/service.py", start_line=13, end_line=26)
        ),
        lambda: svc.list_dir(snap, "backend/app/users"),
        lambda: svc.structural_grep(snap, "user"),
    ]
    for call in calls:
        before = committed()
        call()
        assert committed() > before, "primitive did not write a COMMIT ledger entry"

    # And each served call left a balanced reservation (net reserved == 0).
    assert ledger.reserved_tokens(scope) == 0
    # The ledger recorded reserve/commit/release triples for the served calls.
    kinds = db.conn.execute(
        "SELECT DISTINCT kind FROM budget_ledger WHERE scope = ? ORDER BY kind;", (scope,)
    ).fetchall()
    assert {r["kind"] for r in kinds} == {
        LedgerEntryKind.RESERVE.value,
        LedgerEntryKind.COMMIT.value,
        LedgerEntryKind.RELEASE.value,
    }
