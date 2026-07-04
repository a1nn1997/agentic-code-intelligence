"""Deterministic token/byte accounting for retrieval.

Every retrieval primitive must charge the ledger a cost that is a **pure
function of the bytes it actually returns** — so the same query on the same
snapshot always costs the same (replay determinism, Phase 4) and reading a span
always costs strictly less than reading the whole file (the budget lever that
makes an over-context repo workable).

The formula is intentionally simple and integer-only (no float drift, no
tokenizer dependency that could vary across machines/versions):

    token_cost = PER_CALL_OVERHEAD + ceil(byte_count / BYTES_PER_TOKEN)

where ``byte_count`` is the UTF-8 length of the **post-redaction** content the
caller receives. ``PER_CALL_OVERHEAD`` is a small fixed charge so that even a
zero-byte result costs something (a tool call is never free), and it is equal
across primitives so cost differences come only from returned bytes — which is
what lets a span (fewer bytes) provably undercut a whole file (more bytes).

``BYTES_PER_TOKEN = 4`` is the standard ~4-chars-per-token rule of thumb; the
exact constant does not matter for correctness, only that it is fixed and that
cost is monotonic in returned bytes.
"""

from __future__ import annotations

import math

# Fixed, primitive-independent constants. Monotonicity in returned bytes is the
# only property the oracles depend on; the exact values are a defensible default.
BYTES_PER_TOKEN = 4
PER_CALL_OVERHEAD = 1


def byte_count(content: str) -> int:
    """UTF-8 byte length of the content actually returned to the caller."""
    return len(content.encode("utf-8"))


def token_cost(content: str) -> int:
    """Deterministic integer token cost for returning ``content``.

    Monotonic in ``byte_count(content)``: more bytes never costs fewer tokens,
    so a span (a slice of a file) always costs ``<=`` the whole file, and
    strictly ``<`` whenever the span omits any bytes — which every real span
    does. Pure function ⇒ identical across replays.
    """
    return PER_CALL_OVERHEAD + math.ceil(byte_count(content) / BYTES_PER_TOKEN)
