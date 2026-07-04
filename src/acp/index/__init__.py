"""Structural code index (Python + TypeScript).

Internal contract behind the module boundary: consumers/model never touch it —
Phase 2's retrieval primitives read it. Public surface:

* :class:`~acp.index.model.Index` — the whole-repo index + cross-file queries.
* :class:`~acp.index.model.Symbol` / :class:`~acp.index.model.Reference` /
  :class:`~acp.index.model.ImportEdge` / :class:`~acp.index.model.FileIndex`.
* :class:`~acp.index.builder.IndexBuilder` — full build + incremental re-index.
"""

from acp.index.builder import IndexBuilder
from acp.index.model import FileIndex, ImportEdge, Index, Reference, Symbol

__all__ = [
    "IndexBuilder",
    "Index",
    "FileIndex",
    "Symbol",
    "Reference",
    "ImportEdge",
]
