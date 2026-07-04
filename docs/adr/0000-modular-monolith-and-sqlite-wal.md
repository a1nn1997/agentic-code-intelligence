# ADR-0000 — Modular monolith + separated sandbox tier; SQLite (WAL) as the state and serialization point

- **Status:** Accepted
- **Phase:** 0
- **Date:** 2026-07-03

## Context

The platform must be multi-user, concurrent, budget-enforced, and *runnable
locally with one command and no external keys*. It must also keep the index,
sandbox, and model unreachable by consumers, and survive process kills without
losing work, double-charging, or re-applying side effects. Two structural
choices made at Phase 0 shape everything downstream: the process/service
topology, and the state store.

## Decision

1. **Modular monolith + a separated Docker sandbox tier.** All control-plane
   modules (`gateway`, `retrieval`, `orchestrator`, `workspace`,
   `model_gateway`, `db`) run in one process behind bounded `Protocol`/ABC
   interfaces. Only `gateway` binds a port. Untrusted build/test execution runs
   in a physically separate Docker tier on a private network with no
   host-published port.

2. **SQLite in WAL mode with `BEGIN IMMEDIATE` as the single serialization
   point.** All state — users, hashed API keys, workspaces, tasks, the
   append-only journal, the append-only budget ledger, artifacts — lives in
   SQLite. Writers take the write lock up front (`BEGIN IMMEDIATE`) so
   concurrent writers serialize cleanly instead of failing late. All SQL is
   confined to `acp.db` repositories; no business module issues raw SQL.
   **Connection model (per-thread, B-CRIT-1):** `BEGIN IMMEDIATE` only
   serializes writers across *separate* connections — two threads sharing one
   `sqlite3.Connection` collide inside `BEGIN IMMEDIATE` ("cannot start a
   transaction within a transaction") or one COMMIT flushes another's
   half-written work. Because Starlette runs our sync endpoints on a threadpool,
   the `Database` facade therefore hands each thread its own WAL connection
   (thread-local factory, identical pragmas), tracking them for orderly
   `close()`. The public surface (`conn`/`immediate`/`close`) is unchanged; the
   correctness property the ledger/journal rest on now actually holds under
   concurrent requests.

## Alternatives considered

- **N networked microservices.** Rejected for Phase 0: more failure surfaces,
  more first-run wiring, and no isolation benefit that the separated sandbox
  tier doesn't already provide for the one truly dangerous workload. The
  interface seams mean we *can* split later without rewriting callers.
- **Postgres/Redis from day one.** Rejected: adds a required external service,
  breaking "runs first go, keyless." SQLite's portability claim (works on SQLite
  ⇒ works on Postgres/Aurora) keeps the door open; the repository layer is the
  swap point.
- **A task queue on Redis/Celery.** Rejected: a SQLite-backed queue with row
  locking + visibility timeout gives at-least-once delivery and crash-resume
  with one fewer dependency.

## Consequences / tradeoffs

- **(+)** One deployable artifact; keyless one-command bring-up; clean in-process
  boundaries; the API-only property holds topologically.
- **(+)** WAL gives concurrent readers + one writer; `BEGIN IMMEDIATE` makes the
  journal/ledger safe under concurrent tasks — the substrate for
  effectively-once (ADR-0004).
- **(−)** Shared process = shared blast radius for the control plane. Mitigated
  because untrusted code never runs in it — that is pushed to the sandbox tier.
- **(−)** SQLite single-writer throughput is a ceiling. Acceptable at
  take-home/demo scale; the repository seam is the documented migration path to
  Postgres if throughput demands it.
