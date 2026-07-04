"""Phase-0 stub for retrieval. Real tree-sitter indexing lands in Phase 1 and
budgeted primitives in Phase 2."""

from __future__ import annotations

from acp.common.errors import NotImplementedInPhase
from acp.retrieval.interface import RetrievalResult, SnapshotRef, SpanRef, Symbol


class StubRetrievalService:
    """Satisfies :class:`acp.retrieval.interface.RetrievalService` structurally."""

    def search_symbols(self, snap: SnapshotRef, query: str, limit: int = 20) -> list[Symbol]:
        raise NotImplementedInPhase("retrieval.search_symbols lands in Phase 1/2")

    def definition(self, snap: SnapshotRef, name: str) -> Symbol | None:
        raise NotImplementedInPhase("retrieval.definition lands in Phase 1/2")

    def references(self, snap: SnapshotRef, name: str) -> list[SpanRef]:
        raise NotImplementedInPhase("retrieval.references lands in Phase 1/2")

    def read_span(self, snap: SnapshotRef, span: SpanRef) -> RetrievalResult:
        raise NotImplementedInPhase("retrieval.read_span lands in Phase 2")

    def list_dir(self, snap: SnapshotRef, path: str) -> RetrievalResult:
        raise NotImplementedInPhase("retrieval.list_dir lands in Phase 2")

    def structural_grep(self, snap: SnapshotRef, pattern: str, limit: int = 50) -> list[SpanRef]:
        raise NotImplementedInPhase("retrieval.structural_grep lands in Phase 2")
