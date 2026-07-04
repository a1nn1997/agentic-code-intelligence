"""Structural-index oracle: cross-file / cross-language symbol resolution, the
import graph, deterministic serialization, and the incremental-equivalence
invariant. Also verifies the two adversarial planted items behave: the docstring
injection is never treated as a code reference, and secret *values* never enter
the index. No model/network calls."""

from __future__ import annotations

from pathlib import Path

import pytest

from acp.index import IndexBuilder

pytestmark = pytest.mark.unit

_SERVICE = "backend/app/users/service.py"


# --- Oracle clause 1: one definition resolves to ALL call sites across files -


def test_python_symbol_resolves_to_all_call_sites(sample_repo: Path) -> None:
    idx = IndexBuilder().build(sample_repo)
    defs = idx.definitions("serialize_user", "python")
    assert [s.file_path for s in defs] == [_SERVICE]  # defined in exactly one file
    assert idx.reference_files("serialize_user", "python") == [
        "backend/app/reports/export.py",
        "backend/app/users/api.py",
        "backend/app/users/service.py",
        "backend/tests/test_users.py",
    ]


def test_typescript_symbol_resolves_to_all_call_sites(sample_repo: Path) -> None:
    idx = IndexBuilder().build(sample_repo)
    defs = idx.definitions("formatUser", "typescript")
    assert [s.file_path for s in defs] == ["frontend/src/models/user.ts"]
    assert idx.reference_files("formatUser", "typescript") == [
        "frontend/src/api/usersClient.ts",
        "frontend/src/components/userList.ts",
        "frontend/src/pages/usersPage.ts",
        "frontend/tests/users.test.ts",
    ]


def test_both_languages_are_indexed(sample_repo: Path) -> None:
    idx = IndexBuilder().build(sample_repo)
    stats = idx.stats()
    assert stats["files_python"] >= 5
    assert stats["files_typescript"] >= 4


# --- Oracle clause 3: import graph links importer -> imported, both languages -


def test_import_graph_links_python_and_typescript(sample_repo: Path) -> None:
    idx = IndexBuilder().build(sample_repo)
    edges = set(idx.resolved_import_edges())
    # Python importer -> imported
    assert ("backend/app/users/api.py", "backend/app/users/service.py") in edges
    assert ("backend/app/reports/export.py", "backend/app/users/service.py") in edges
    # TypeScript importer -> imported
    assert ("frontend/src/components/userList.ts", "frontend/src/models/user.ts") in edges
    assert ("frontend/src/api/usersClient.ts", "frontend/src/models/user.ts") in edges


# --- Oracle clause 4: determinism -------------------------------------------


def test_index_is_deterministic(sample_repo: Path) -> None:
    a = IndexBuilder().build(sample_repo).serialize()
    b = IndexBuilder().build(sample_repo).serialize()
    assert a == b


def test_serialized_index_round_trips(sample_repo: Path) -> None:
    from acp.index.model import Index

    idx = IndexBuilder().build(sample_repo)
    blob = idx.serialize()
    assert Index.from_serialized(blob).serialize() == blob


# --- Oracle clause 2: incremental re-index == full rebuild, only 1 file parsed


def test_incremental_reindex_equals_full_rebuild(sample_repo: Path) -> None:
    full = IndexBuilder()
    idx = full.build(sample_repo)
    total_files = full.files_parsed
    assert total_files == 15  # sanity: a full build parses every indexed file

    # Edit one file on disk.
    target = sample_repo / _SERVICE
    target.write_text(
        target.read_text(encoding="utf-8")
        + "\n\ndef added_symbol() -> int:\n    return 1\n",
        encoding="utf-8",
    )

    # Incremental: re-parse exactly the edited file.
    inc = IndexBuilder()
    inc.reindex_file(idx, sample_repo, _SERVICE)
    assert inc.files_parsed == 1  # ONLY the affected file was re-parsed

    # The incremental index picked up the change...
    assert [s.file_path for s in idx.definitions("added_symbol")] == [_SERVICE]

    # ...and equals a full rebuild of the edited tree, byte-for-byte.
    rebuilt = IndexBuilder().build(sample_repo)
    assert idx.serialize() == rebuilt.serialize()


def test_incremental_handles_file_deletion(sample_repo: Path) -> None:
    inc = IndexBuilder()
    idx = inc.build(sample_repo)
    target = sample_repo / "frontend/src/pages/usersPage.ts"
    target.unlink()
    inc.reindex_file(idx, sample_repo, "frontend/src/pages/usersPage.ts")
    rebuilt = IndexBuilder().build(sample_repo)
    assert idx.serialize() == rebuilt.serialize()


# --- Adversarial planted items ----------------------------------------------


def test_docstring_injection_is_not_a_code_reference(sample_repo: Path) -> None:
    """The planted prompt-injection lives in a docstring. A structural index
    (unlike grep) must not surface its words as references to any symbol."""
    raw = (sample_repo / _SERVICE).read_text(encoding="utf-8")
    assert "ignore all previous instructions" in raw  # the payload IS present
    idx = IndexBuilder().build(sample_repo)
    # Words that occur ONLY inside the docstring resolve to zero references.
    assert idx.references_of("instructions") == []
    assert idx.references_of("reveal") == []


def test_secret_values_never_enter_the_index(sample_repo: Path) -> None:
    idx = IndexBuilder().build(sample_repo)
    blob = idx.serialize()
    # The fake secret VALUES from config.py must not be present anywhere.
    assert "sk-live-1234567890abcdefADVERSARIALdeadbeef" not in blob
    assert "sk_test_51Hxfake" not in blob
    # ...but the symbol NAME is indexed (structure, not literal contents).
    assert any(s.name == "SECRET_KEY" for s in idx.all_symbols())
