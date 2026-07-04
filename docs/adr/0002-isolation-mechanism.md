# ADR-0002 — Isolation mechanism: scoped accessors + per-user workspaces

- **Status:** **Accepted** (workspace-scoping portion decided Phase 1; per-task
  worktrees finalized Phase 4).
- **Phase:** 1 (workspace scoping) → 4 (per-task worktrees, WAL + `BEGIN
  IMMEDIATE` serialization).
- **Deciders:** isolation/security role.

## Context

Hard requirement #2: **user isolation by construction**, enforced server-side on
every read and write. It must not be possible for one user to address, read,
index, or mutate another user's data — and we must actively try to break it. Two
layers of state need isolating: rows in SQLite, and repo files on disk.

## Decision (Phase 1 portion)

**Scoped accessors, no unscoped getter on the consumer path.** Every
consumer-facing accessor is keyed on `user_id`; the SQL filters on it
(`WorkspacesRepo.get/list`, `TasksRepo.get`). There is deliberately no
`get_any_workspace(id)`. The one unscoped reader (`TasksRepo.get_for_resume`) is
reachable only by the server-side crash-resume path, never by a consumer.

**Per-user workspace paths derived from `(user_id, workspace_id)` together.**
`WorkspaceServiceImpl` stores each ingested repo at
`<root>/<user_id>/<workspace_id>/repo`. Every operation first calls
`get_workspace(user_id, …)` as an ownership gate, so a non-owned id fails with
`NotFound` before any disk access. "Not owned" and "does not exist" are returned
identically, so probing cannot confirm another user's workspace exists.

**Containment checks as defense-in-depth.** Resolved paths must lie under the
user's root (crafted ids / `../` edit paths → `IsolationViolation`); archive
ingestion is zip-slip / tar-escape guarded (`tarfile` `filter="data"`, explicit
zip member check).

**`IsolationViolation` is a tripwire, not routine flow.** The scoped accessors
make cross-user addressing unrepresentable; if the exception ever fires, a
safety invariant was bypassed and it is treated as an incident.

## Alternatives considered

- **RLS-style per-row checks only (no scoped accessor).** Kept as the
  defense-in-depth layer (`assert_owned`), but not as the *primary* mechanism:
  a check you must remember to call is one you can forget. Making the unsafe
  operation unrepresentable in the interface is stronger than checking for it.
- **One shared workspace directory keyed by workspace_id alone.** Rejected: a
  bare `workspace_id` becomes a capability that addresses any user's files;
  isolation would then rest entirely on remembering to check ownership.
- **OS-level per-user accounts / containers for workspace storage.** Stronger,
  but heavy for a modular monolith and orthogonal to the real dangerous
  workload (running untrusted code), which is already isolated in the Docker
  sandbox tier (ADR-0003). Documented as a production hardening path.

## Consequences

- **Positive.** Cross-user addressing is not expressible on the consumer path;
  verified adversarially (a second user cannot get/index/reach/edit/see another
  user's workspace). Filesystem and DB isolation share one ownership gate.
- **Negative / limits.** Isolation is process-internal (same UID on disk); it
  defends against *logical* cross-user access, not a full OS compromise. Org /
  tenant hierarchy and where the model breaks are written in Phase 6.
## Decision (Phase 4 portion — per-task worktree isolation)

**Each task operates on its own materialized worktree, not the base repo.**
`WorkspaceServiceImpl.open_worktree(user_id, workspace_id, task_id)` vends a
per-task checkout at `<root>/<user_id>/<workspace_id>/worktrees/<task_id>`,
created by copying the base repo on first open. Because the path is derived from
`(user_id, workspace_id, task_id)`, two concurrent tasks — and the base — occupy
**physically disjoint** directories, so a run's edits land only in its own
worktree and can never clobber the base or a peer. The agent loop applies all
span-patches into this worktree and points the sandbox at it for verification.

**Resume-safe by construction.** `open_worktree` snapshots only on first open; a
resumed run of the same task re-opens the *same* directory and finds its
already-applied edits intact — the copy is not redone. Verified in
`test_worktree_isolation.py` (disjoint dirs; edit in one leaves base + peer
byte-identical; re-open idempotent).

**Serialization point.** SQLite **WAL + `BEGIN IMMEDIATE`** (all writes go
through `Database.immediate()`) is the serialization point for the append-only
`journal` and `budget_ledger` — the tables that back resume and no-double-charge
(ADR-0004). Concurrent tasks writing their own journal/ledger rows are
serialized safely at the DB.

**Scope of the concurrency story (honest).** Real git worktrees from a *bare
mirror* — with commit-back guarded by an advisory lock + base-commit check
(rebase-reverify or reject) — are the production form; here the Phase-1
content-hash snapshot id stands in for a commit and a worktree is a copy.
`WorktreeHandle.base_commit` records the head at open time so the base-commit
conflict check has its input. **The agent loop commits back through this guarded
path (A9):** on VERIFIED_SUCCESS in APPLY mode, `AgentLoop._commit_if_apply`
calls `commit_worktree(base_commit=handle.base_commit)`; a `ConflictError` (the
base advanced under a concurrent commit) terminates the loser cleanly as
`gave_up` with a "commit conflict" reason — the **reject** policy (never silent
clobber). What is proven for adversarial scenario 6 is now end-to-end via the
production flow: two concurrent APPLY tasks resolve to exactly one commit + one
`ConflictError` (`test_apply_commit_concurrency.py`), not merely that they occupy
disjoint directories.
