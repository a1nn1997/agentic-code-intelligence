"""Agentic Code-Intelligence Platform (ACP).

A modular monolith: bounded modules run in one control-plane process, with a
separate Docker sandbox tier for untrusted execution. Only the ``gateway``
module publishes an HTTP port; every other module is an in-process library with
a Protocol/ABC interface so it can be split out later without changing callers.

Phase 0 ships interfaces and stubs only — no index/retrieval/sandbox/agent
logic. Each stub raises :class:`acp.common.errors.NotImplementedInPhase` so an
accidental call fails loudly instead of silently returning a wrong answer.
"""

__version__ = "0.0.0"
