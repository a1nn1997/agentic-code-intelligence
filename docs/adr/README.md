# Architecture Decision Records

One ADR per hard decision the spec explicitly names, **written in the phase
where the decision is made — never backfilled**. Each ADR follows:
*context → decision → alternatives considered → consequences/tradeoffs.*

| ADR | Decision | Phase | Status |
|---|---|---|---|
| [0000](0000-modular-monolith-and-sqlite-wal.md) | Modular monolith + separated sandbox tier; SQLite WAL as state/serialization point | 0 | Accepted |
| [0001](0001-retrieval-model.md) | Retrieval model — structural index + budgeted primitives; why not embeddings-only | 1 → 2 | Proposed (structural index landed P1; primitives finalize P2) |
| [0002](0002-isolation-mechanism.md) | Isolation mechanism — scoped accessor + per-user workspaces; per-task worktree; WAL + `BEGIN IMMEDIATE` | 1 → 4 | Proposed (workspace scoping landed P1; worktrees finalize P4) |
| [0003](0003-sandbox-technology.md) | Sandbox technology — Docker baseline; sandbox-as-contract; gVisor/Firecracker prod path | 3 → 8 | Accepted (reference runner + contract landed P3; parity in P8) |
| 0004 | Delivery / consistency guarantee — at-least-once + idempotent effects = effectively-once | 4 | Pending (journal/ledger seed in 0000) |
