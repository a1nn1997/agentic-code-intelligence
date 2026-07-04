"""The structural-index data model.

This is the index's *internal contract* (Phase 1 deliverable): the model/consumer
never touches it directly — Phase 2's retrieval primitives read it, Phase 4's
agent acts only through those primitives. The design goals encoded here:

* **Per-file contributions are self-contained.** Everything the index knows is
  partitioned by file into :class:`FileIndex`. Cross-file answers (a symbol's
  references, the import graph) are *derived on query* from these partitions,
  never stored redundantly. That is what makes incremental re-index correct: to
  re-index a file we replace exactly one partition and nothing else can drift.

* **Serialization is a pure, canonical function of the partitions.** Lists are
  sorted by stable keys and JSON is emitted with sorted keys and no incidental
  whitespace, so the same snapshot always yields byte-identical output. Two
  facts fall out for free: determinism (same snapshot → same bytes) and the
  incremental-equivalence invariant (incremental result == full-rebuild result,
  compared byte-for-byte).
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel

SymbolKind = Literal["function", "class", "method", "type", "variable"]
RefKind = Literal["call", "use", "import"]


class Symbol(BaseModel):
    """A defined symbol located in a file, with its full span."""

    name: str
    kind: SymbolKind
    language: str
    file_path: str
    start_line: int  # 1-based, inclusive
    end_line: int  # 1-based, inclusive
    start_col: int  # 0-based
    end_col: int

    def sort_key(self) -> tuple[int, int, str, str]:
        return (self.start_line, self.start_col, self.name, self.kind)


class Reference(BaseModel):
    """A use of an identifier — the raw material for cross-file reference
    resolution. Excludes the defining occurrence of a symbol (that is a Symbol,
    not a Reference). Never records identifiers inside comments or string
    literals, because tree-sitter models those as ``comment`` / ``string`` nodes,
    not ``identifier`` nodes — so the planted injection in a docstring can never
    masquerade as a code reference."""

    name: str
    language: str
    file_path: str
    line: int  # 1-based
    col: int  # 0-based
    ref_kind: RefKind

    def sort_key(self) -> tuple[int, int, str, str]:
        return (self.line, self.col, self.name, self.ref_kind)


class ImportEdge(BaseModel):
    """An import statement, with the module string and — when it points at a
    file inside this repo — the resolved importer→imported edge."""

    from_path: str
    module: str  # raw module string as written ("app.users.service", "../models/user")
    to_path: str | None  # resolved repo-relative path, or None if external/unresolved
    line: int

    def sort_key(self) -> tuple[int, str, str]:
        return (self.line, self.module, self.to_path or "")


class FileIndex(BaseModel):
    """Everything the index knows about a single file — a self-contained
    partition of the whole index."""

    path: str  # repo-relative, forward slashes
    language: str
    content_hash: str  # sha256 of the exact bytes indexed
    symbols: list[Symbol]
    references: list[Reference]
    imports: list[ImportEdge]

    def canonical(self) -> dict[str, object]:
        """A canonically-ordered plain dict for deterministic serialization."""
        return {
            "path": self.path,
            "language": self.language,
            "content_hash": self.content_hash,
            "symbols": [
                s.model_dump() for s in sorted(self.symbols, key=Symbol.sort_key)
            ],
            "references": [
                r.model_dump() for r in sorted(self.references, key=Reference.sort_key)
            ],
            "imports": [
                i.model_dump() for i in sorted(self.imports, key=ImportEdge.sort_key)
            ],
        }


class Index:
    """The whole-repo structural index: a map of file path → :class:`FileIndex`,
    plus cross-file query methods computed from those partitions on demand.

    Query methods are deliberately *not* memoized: they recompute from ``files``
    every call, so they can never drift from the partitions after an incremental
    update. For a Phase-1 sample repo this is trivially cheap; Phase 2 may back
    the hot queries with prebuilt maps behind the same interface.
    """

    def __init__(self, files: dict[str, FileIndex] | None = None) -> None:
        self.files: dict[str, FileIndex] = files or {}

    # --- mutation (the only writer is the builder) ---------------------------
    def put_file(self, fi: FileIndex) -> None:
        self.files[fi.path] = fi

    def drop_file(self, path: str) -> None:
        self.files.pop(path, None)

    # --- cross-file queries --------------------------------------------------
    def all_symbols(self) -> list[Symbol]:
        out = [s for fi in self.files.values() for s in fi.symbols]
        return sorted(out, key=lambda s: (s.file_path, *s.sort_key()))

    def definitions(self, name: str, language: str | None = None) -> list[Symbol]:
        """All symbols defined with ``name`` (optionally restricted to a language)."""
        out = [
            s
            for s in self.all_symbols()
            if s.name == name and (language is None or s.language == language)
        ]
        return out

    def references_of(self, name: str, language: str | None = None) -> list[Reference]:
        """Every reference (call site / use) of ``name`` across all files.

        Reference resolution is name- and language-scoped: a Python definition
        resolves against Python references, a TypeScript definition against
        TypeScript references. This is the honest resolution model for a
        structural index without full type inference; its one limitation
        (two same-named symbols in one language collapse) is documented in
        ADR-001 and does not affect the sample repo's unique symbols.
        """
        out = [
            r
            for fi in self.files.values()
            for r in fi.references
            if r.name == name and (language is None or r.language == language)
        ]
        return sorted(out, key=lambda r: (r.file_path, *r.sort_key()))

    def reference_files(self, name: str, language: str | None = None) -> list[str]:
        """Distinct files that reference ``name`` (sorted)."""
        return sorted({r.file_path for r in self.references_of(name, language)})

    def import_edges(self) -> list[ImportEdge]:
        out = [e for fi in self.files.values() for e in fi.imports]
        return sorted(out, key=lambda e: (e.from_path, *e.sort_key()))

    def resolved_import_edges(self) -> list[tuple[str, str]]:
        """(importer, imported) pairs that resolve to a file inside the repo."""
        return sorted(
            {(e.from_path, e.to_path) for e in self.import_edges() if e.to_path}
        )

    # --- serialization -------------------------------------------------------
    def serialize(self) -> str:
        """Canonical JSON. Byte-stable for a given set of partitions — the basis
        for the determinism and incremental-equivalence oracles."""
        payload = {
            "version": 1,
            "files": [
                self.files[p].canonical() for p in sorted(self.files.keys())
            ],
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def digest(self) -> str:
        """A short content id for the whole index (sha256 of the serialization)."""
        return hashlib.sha256(self.serialize().encode("utf-8")).hexdigest()

    @classmethod
    def from_serialized(cls, blob: str) -> Index:
        """Rebuild an Index from :meth:`serialize` output. Round-trips exactly:
        ``Index.from_serialized(idx.serialize()).serialize() == idx.serialize()``."""
        payload = json.loads(blob)
        files = {
            fi["path"]: FileIndex.model_validate(fi) for fi in payload.get("files", [])
        }
        return cls(files)

    def stats(self) -> dict[str, int]:
        by_lang: dict[str, int] = {}
        for fi in self.files.values():
            by_lang[fi.language] = by_lang.get(fi.language, 0) + 1
        return {
            "files": len(self.files),
            "symbols": sum(len(fi.symbols) for fi in self.files.values()),
            "references": sum(len(fi.references) for fi in self.files.values()),
            "imports": sum(len(fi.imports) for fi in self.files.values()),
            "resolved_import_edges": len(self.resolved_import_edges()),
            **{f"files_{lang}": n for lang, n in sorted(by_lang.items())},
        }
