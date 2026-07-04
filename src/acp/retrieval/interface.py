"""Interface contract for the budgeted structural-retrieval primitives.

These six primitives are the *entire* surface the agent has onto the codebase.
Because retrieval is span-oriented and symbol-aware, the model never needs the
whole file — reading a span always costs strictly less budget than reading a
file, which is what makes budget-bounded work on an over-context repo possible.

Every result carries ``token_cost`` and ``byte_count`` so the caller can meter
against the ledger *before and after* each call. Secret redaction happens inside
these implementations, at the retrieval boundary (Phase 2), so a planted secret
can never reach a prompt.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field


class SnapshotRef(BaseModel):
    """Identifies the exact workspace state a retrieval query runs against, so
    the same query on the same snapshot returns byte-identical output
    (deterministic replay)."""

    workspace_id: str
    commit: str


class Symbol(BaseModel):
    """A defined symbol (function, class, method, type) located in the index."""

    name: str
    kind: str = Field(description="function | class | method | type | variable")
    language: str
    file_path: str
    start_line: int
    end_line: int


class SpanRef(BaseModel):
    """A byte/line span within a file — the unit of both reads and edits."""

    file_path: str
    start_line: int
    end_line: int


class RetrievalResult(BaseModel):
    """A metered retrieval payload. ``content`` is post-redaction data only."""

    content: str
    byte_count: int
    token_cost: int
    truncated: bool = False


@runtime_checkable
class RetrievalService(Protocol):
    """The fixed, allow-listed retrieval toolset the agent may call."""

    def search_symbols(self, snap: SnapshotRef, query: str, limit: int = 20) -> list[Symbol]:
        """Fuzzy/exact symbol-name search across the indexed languages."""
        ...

    def definition(self, snap: SnapshotRef, name: str) -> Symbol | None:
        """Resolve a symbol name to its defining span."""
        ...

    def references(self, snap: SnapshotRef, name: str) -> list[SpanRef]:
        """All call sites / uses of a symbol across files (drives multi-file edits)."""
        ...

    def read_span(self, snap: SnapshotRef, span: SpanRef) -> RetrievalResult:
        """Read a bounded span. Always cheaper than reading the whole file."""
        ...

    def list_dir(self, snap: SnapshotRef, path: str) -> RetrievalResult:
        """List a directory's entries (structure, not contents)."""
        ...

    def structural_grep(self, snap: SnapshotRef, pattern: str, limit: int = 50) -> list[SpanRef]:
        """Structure-aware search returning spans, not raw byte offsets."""
        ...
