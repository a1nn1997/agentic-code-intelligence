# ADR-0001 — Retrieval model: structural index over embeddings-only

- **Status:** **Accepted** (started Phase 1 with the structural index; finalized
  in Phase 2 once the budgeted primitives + secret redaction landed on top).
- **Phase:** 1 (structural index) → 2 (budgeted primitives, accounting, redaction).
- **Deciders:** index/retrieval role.

## Context

The platform's defining constraint (planted by the spec) is that the target repo
**does not fit in the model's context window**. The retrieval layer decides what
the model sees; get it wrong and every downstream property — budget, latency,
correctness of multi-file edits — degrades. We must resolve a symbol to *all* its
call sites across files and across languages (Python + TypeScript), cheaply and
completely, and we must never let retrieved code become an instruction or leak a
secret.

## Decision

Retrieval is primarily a **structural index built with tree-sitter**, owned
in-process (`src/acp/index/`), exposing:

1. a **symbol table** (functions, classes, methods, types, module-level
   variables) with exact spans;
2. **references / call sites** by name, language-scoped;
3. an **import/file graph** resolving importer→imported to real repo files.

The index is **partitioned by file**; cross-file queries are derived from the
partitions on demand. Serialization is **canonical and byte-stable**, giving two
free properties: determinism (same snapshot → same bytes, for Phase-2 replay) and
an **incremental-equivalence invariant** (a single-file re-index is byte-for-byte
equal to a full rebuild). Semantic/embedding search (Chroma, in-memory) is
allowed **only as an additive complement** in Phase 2, never as the primary or a
replacement.

## Alternatives considered

- **Embeddings-only (naive RAG).** Rejected as the primary model. No symbol
  identity (cannot guarantee *all* call sites — a missed site ships a broken
  build), chunk boundaries cut across scopes, cost scales with retrieved text
  rather than with the symbols an edit touches, and similarity hits happily pull
  secrets/injections into the prompt. Good for fuzzy "where is X-ish" — hence
  kept as an optional complement, not the spine.
- **LSIF (precomputed index format).** Rich and standard, but generation depends
  on per-language indexers/toolchains we would not own, it is heavy for a
  take-home, and incremental update is awkward. We want to *own the core*.
- **ctags / grep.** ctags gives definitions but weak/again-grep-based reference
  resolution; grep matches inside comments and strings — which would surface the
  planted docstring injection as a "reference" and leak secret values. We wanted
  precisely the opposite behavior, which an AST gives for free.
- **LSP (language servers) at query time.** Most semantically accurate, but adds
  a long-lived per-language server process per workspace, latency, and lifecycle
  complexity; harder to make deterministic/replayable. tree-sitter gives us
  enough structure for symbol/reference/import resolution without that weight,
  and is GC-safe from Python (see ADR-0000 language rationale).

## Consequences

- **Positive.** Complete cross-file/-language resolution; comment/string content
  never indexed (injection defense + secrets never enter the index);
  deterministic, replayable serialization; cheap incremental re-index with a
  proven equivalence invariant; no third-party framework hiding the core.
- **Negative / limits.** Reference resolution is **name- + language-scoped**
  (no full type inference), so two distinct same-named symbols in one language
  would collapse into one reference set. Acceptable for Phase-1/5 (the rename
  target is unique) and documented; a scope/type-aware resolver is the future
  path if precision demands it. tree-sitter parses, it does not type-check —
  semantic correctness of an edit is proven by the **sandbox** (ADR-0003), not
  by the index.
## Accounting model (finalized Phase 2)

The six primitives (`search_symbols`, `definition`, `references`, `read_span`,
`read_file`, `list_dir`, `structural_grep`) each return a metered payload and
charge the append-only `budget_ledger`. **Cost is a pure integer function of the
bytes actually returned:** `PER_CALL_OVERHEAD + ceil(byte_count / BYTES_PER_TOKEN)`
with fixed, primitive-independent constants. Two properties follow by
construction and are asserted by the Phase-2 oracle:

- **Span < file.** Identical constants across primitives ⇒ cost differs only by
  returned bytes ⇒ a span always costs strictly less than the file it slices.
  The agent pays for symbols-touched, not repo bulk.
- **Deterministic accounting.** Cost depends only on returned post-redaction
  bytes, and results are pinned to a `SnapshotRef` ⇒ same query on same snapshot
  yields byte-identical content *and* identical charges — the substrate for
  Phase-4 replay (no double-charge, no divergence on resume).

Charging is a **RESERVE → COMMIT → RELEASE** triple behind a single choke point,
with a **pre-op check that refuses an over-budget call before any ledger write**
(raises `BudgetExceeded`, returns no content, leaves row count and balance
unchanged). **Secret redaction** runs inside the same choke point, on every
content-returning primitive, before content leaves the module — the first layer
of the secret-hygiene story (sandbox egress-deny, ADR-0003, is the backstop).

The two limitations above (name+language-scoped resolution; index-as-single-JSON
scaling seam) are carried forward explicitly into DESIGN.md §2 with their failure
modes, and both are fixable behind the unchanged `Index` / `RetrievalService`
interfaces.
