"""Structural code retrieval: a tree-sitter symbol table + reference/import
graph over the workspace, exposed as a small set of budget-aware primitives.

This is the heart of the "beats naive RAG" claim (ADR-001): the agent navigates
structure and reads spans, never dumping files into the model. Every primitive
is token-accounted against the ledger (Phase 2). Phase 0 defines the interface.
"""

from acp.retrieval.interface import RetrievalService
from acp.retrieval.service import RetrievalServiceImpl
from acp.retrieval.stub import StubRetrievalService

__all__ = ["RetrievalService", "RetrievalServiceImpl", "StubRetrievalService"]
