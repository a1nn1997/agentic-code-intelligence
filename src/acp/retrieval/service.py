"""RetrievalService — Phase-2 implementation over the Phase-1 structural index.

The six primitives are the *entire* surface the agent has onto a repo. Three
properties are load-bearing and each maps to an oracle clause:

* **Metered before served.** Every call computes its result and the result's
  cost first (pure, no side effect), then goes through
  :meth:`_charge` which does a **reserve → commit** against the append-only
  ``budget_ledger``. If the cost would exceed the scope's remaining budget the
  call raises :class:`BudgetExceeded` and writes **nothing** to the ledger — an
  over-budget retrieval is refused, never served-then-charged.

* **Deterministic.** A result is a pure function of (index snapshot, query), and
  cost is a pure function of the returned post-redaction bytes. Same query on
  the same snapshot ⇒ byte-identical content AND identical accounting — the
  basis for Phase-4 replay.

* **Redacted at the boundary.** Any primitive that returns file *content*
  (``read_span``, ``list_dir`` never returns content; ``structural_grep``
  returns spans) runs it through :mod:`acp.retrieval.redaction` before it leaves
  this layer, and records the redaction as a metered event.

Retrieval is **read-only** and snapshot-scoped: it loads the persisted index for
the workspace and answers from it. Reference resolution is **name- and
language-scoped** (inherited from the Phase-1 index, ADR-0001) — its one failure
mode (two distinct same-named symbols in one language collapse into one
reference set) is restated in DESIGN.md §2.
"""

from __future__ import annotations

from pathlib import Path

from acp.common.errors import NotFound
from acp.common.logging import get_logger
from acp.db import Database
from acp.db.repositories import LedgerRepo
from acp.index.model import Index
from acp.retrieval.accounting import byte_count, token_cost
from acp.retrieval.interface import (
    RetrievalResult,
    SnapshotRef,
    SpanRef,
    Symbol,
)
from acp.retrieval.redaction import RedactionResult, redact
from acp.workspace.service import WorkspaceServiceImpl

_log = get_logger(__name__)


class RetrievalServiceImpl:
    """Concrete :class:`acp.retrieval.interface.RetrievalService`.

    Scoped to a single ``user_id``: every snapshot it resolves is loaded through
    the user-scoped :class:`WorkspaceServiceImpl`, so retrieval cannot address a
    workspace the user does not own (isolation by construction, inherited).
    Budget is charged to a caller-supplied ``scope`` string (``task:<id>`` or
    ``user:<id>``) with a hard ``budget_tokens`` ceiling.
    """

    def __init__(
        self,
        db: Database,
        workspace_root: str | Path,
        user_id: str,
        *,
        scope: str,
        budget_tokens: int,
    ) -> None:
        self._db = db
        self._ledger = LedgerRepo(db)
        self._workspaces = WorkspaceServiceImpl(db, workspace_root)
        self._user_id = user_id
        self._scope = scope
        self._budget_tokens = budget_tokens
        # Snapshot the index once per (workspace, commit) so repeated queries in
        # one session are answered from an immutable in-memory view — pure ⇒
        # deterministic across calls.
        self._index_cache: dict[tuple[str, str], Index] = {}

    # --- ledger interaction --------------------------------------------------
    def spent_tokens(self) -> int:
        """Committed token spend for this scope so far (for operator display)."""
        return self._ledger.spent_tokens(self._scope)

    def remaining_tokens(self) -> int:
        """Budget left for this scope: ceiling − committed − net-reserved."""
        spent = self._ledger.spent_tokens(self._scope)
        reserved = self._ledger.reserved_tokens(self._scope)
        return self._budget_tokens - spent - reserved

    def _charge(self, tokens: int, *, note: str, step_index: int | None = None) -> None:
        """Reserve → commit ``tokens`` against the ledger, atomically.

        The whole interaction — the budget check AND the RESERVE/COMMIT/RELEASE
        writes — happens inside a **single** ``BEGIN IMMEDIATE`` in
        :meth:`LedgerRepo.charge_atomic`. This closes the B-3 TOCTOU: the old
        body read ``remaining_tokens()`` (a separate read transaction) and then
        wrote three rows in three *separate* write transactions, so two
        concurrent retrievals on one scope could both pass the check and both
        commit, overshooting the ceiling. With the check-and-write under one
        write lock, a second charger blocks until the first commits and then
        sees the post-charge balance — the budget can never be overshot.

        Semantics are unchanged: on refusal :class:`BudgetExceeded` is raised
        and **nothing** is written (the transaction rolls back); on success
        ``spent`` rose by exactly ``tokens`` and net ``reserved`` is 0.
        """
        self._ledger.charge_atomic(
            self._scope,
            tokens,
            self._budget_tokens,
            step_index=step_index,
            note=note,
        )

    def _meter_content(
        self, content: str, *, primitive: str, step_index: int | None
    ) -> RetrievalResult:
        """Redact content, compute its cost, charge the ledger, return the
        metered result. The single boundary all content passes through."""
        red: RedactionResult = redact(content)
        cost = token_cost(red.content)
        note = f"{primitive}:{red.total_redactions}redactions" if red.redacted else primitive
        # Charge FIRST — if over budget this raises and no content is returned.
        self._charge(cost, note=note, step_index=step_index)
        if red.redacted:
            # Log the redaction as a metered event — never the secret value.
            _log.info(
                "retrieval.secret_redacted",
                extra={
                    "primitive": primitive,
                    "scope": self._scope,
                    "redactions": red.total_redactions,
                    "reasons": [e.reason for e in red.events],
                },
            )
        return RetrievalResult(
            content=red.content,
            byte_count=byte_count(red.content),
            token_cost=cost,
        )

    # --- snapshot / index loading -------------------------------------------
    def _index(self, snap: SnapshotRef) -> Index:
        key = (snap.workspace_id, snap.commit)
        if key not in self._index_cache:
            ref = self._workspaces.get_workspace(self._user_id, snap.workspace_id)
            if ref.head_commit != snap.commit:
                # A retrieval is pinned to a specific snapshot; refuse to answer
                # from a drifted index (would break replay determinism).
                raise NotFound(
                    f"snapshot {snap.commit} is not the current head of "
                    f"workspace {snap.workspace_id}"
                )
            self._index_cache[key] = self._workspaces.load_index(
                self._user_id, snap.workspace_id
            )
        return self._index_cache[key]

    def _repo_path(self, snap: SnapshotRef) -> Path:
        return self._workspaces.repo_path(self._user_id, snap.workspace_id)

    # --- primitives ----------------------------------------------------------
    def search_symbols(
        self, snap: SnapshotRef, query: str, limit: int = 20
    ) -> list[Symbol]:
        """Exact-or-substring symbol-name search across indexed languages.

        Metadata-only (names/spans), so the returned payload is metered by the
        canonical text of the hits — never file content, and nothing to redact.
        """
        index = self._index(snap)
        q = query.lower()
        hits = [s for s in index.all_symbols() if q in s.name.lower()][:limit]
        out = [
            Symbol(
                name=s.name,
                kind=s.kind,
                language=s.language,
                file_path=s.file_path,
                start_line=s.start_line,
                end_line=s.end_line,
            )
            for s in hits
        ]
        self._meter_content(
            _symbols_repr(out), primitive="search_symbols", step_index=None
        )
        return out

    def definition(self, snap: SnapshotRef, name: str) -> Symbol | None:
        """Resolve a symbol name to its defining span (first, deterministically
        ordered, definition)."""
        index = self._index(snap)
        defs = index.definitions(name)
        result = (
            Symbol(
                name=defs[0].name,
                kind=defs[0].kind,
                language=defs[0].language,
                file_path=defs[0].file_path,
                start_line=defs[0].start_line,
                end_line=defs[0].end_line,
            )
            if defs
            else None
        )
        self._meter_content(
            _symbols_repr([result] if result else []),
            primitive="definition",
            step_index=None,
        )
        return result

    def references(self, snap: SnapshotRef, name: str) -> list[SpanRef]:
        """All call sites / uses of ``name`` across files (name+language scoped;
        drives multi-file edits in Phase 5)."""
        index = self._index(snap)
        refs = index.references_of(name)
        out = [
            SpanRef(file_path=r.file_path, start_line=r.line, end_line=r.line)
            for r in refs
        ]
        self._meter_content(_spans_repr(out), primitive="references", step_index=None)
        return out

    def read_span(self, snap: SnapshotRef, span: SpanRef) -> RetrievalResult:
        """Read a bounded [start_line, end_line] slice of a file (1-based,
        inclusive). Redacted + metered; strictly cheaper than the whole file
        because it returns strictly fewer bytes."""
        text = self._read_lines(snap, span.file_path, span.start_line, span.end_line)
        return self._meter_content(text, primitive="read_span", step_index=None)

    def read_file(self, snap: SnapshotRef, file_path: str) -> RetrievalResult:
        """Read an entire file. **Possible but deliberately more expensive** than
        a span — it returns every byte, so its cost dominates any span of it.
        This is the escape hatch; spans are the primary path."""
        text = self._read_lines(snap, file_path, 1, None)
        return self._meter_content(text, primitive="read_file", step_index=None)

    def list_dir(self, snap: SnapshotRef, path: str) -> RetrievalResult:
        """List a directory's immediate entries (structure, not contents).

        Names only — cheap, and there is nothing to redact (a filename is not a
        secret value). Directories are marked with a trailing slash."""
        repo = self._repo_path(snap)
        target = (repo / path).resolve()
        if repo.resolve() not in target.parents and target != repo.resolve():
            raise NotFound(f"path escapes workspace: {path}")
        if not target.is_dir():
            raise NotFound(f"not a directory: {path}")
        entries = sorted(
            f"{e.name}/" if e.is_dir() else e.name for e in target.iterdir()
        )
        return self._meter_content("\n".join(entries), primitive="list_dir", step_index=None)

    def structural_grep(
        self, snap: SnapshotRef, pattern: str, limit: int = 50
    ) -> list[SpanRef]:
        """Structure-aware search: match ``pattern`` against **symbol names and
        reference names** in the index (never comment/string bytes), returning
        the spans of matching symbols. Because it reads the index, not raw file
        bytes, it can never surface a match inside a docstring — the planted
        injection is invisible to it."""
        index = self._index(snap)
        pat = pattern.lower()
        hits = [
            SpanRef(
                file_path=s.file_path, start_line=s.start_line, end_line=s.end_line
            )
            for s in index.all_symbols()
            if pat in s.name.lower()
        ][:limit]
        self._meter_content(_spans_repr(hits), primitive="structural_grep", step_index=None)
        return hits

    # --- helpers -------------------------------------------------------------
    def _read_lines(
        self, snap: SnapshotRef, file_path: str, start_line: int, end_line: int | None
    ) -> str:
        """Read [start_line, end_line] (1-based inclusive) of a repo file.

        Path is containment-checked against the workspace repo root, so a
        crafted ``file_path`` cannot escape the workspace.
        """
        repo = self._repo_path(snap)
        target = (repo / file_path).resolve()
        if repo.resolve() not in target.parents and target != repo.resolve():
            raise NotFound(f"path escapes workspace: {file_path}")
        if not target.is_file():
            raise NotFound(f"file not found: {file_path}")
        lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
        lo = max(start_line - 1, 0)
        hi = len(lines) if end_line is None else min(end_line, len(lines))
        return "".join(lines[lo:hi])


# --- canonical metadata representations (deterministic, sorted) -------------


def _symbols_repr(symbols: list[Symbol]) -> str:
    """Stable one-line-per-symbol text used to meter metadata results."""
    return "\n".join(
        f"{s.language}\t{s.kind}\t{s.file_path}:{s.start_line}-{s.end_line}\t{s.name}"
        for s in symbols
    )


def _spans_repr(spans: list[SpanRef]) -> str:
    return "\n".join(f"{s.file_path}:{s.start_line}-{s.end_line}" for s in spans)
