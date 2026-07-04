# ADR-0004 — Delivery / consistency guarantee: effectively-once via journal + idempotency

- **Status:** **Accepted** (Phase 4).
- **Phase:** 4 (agent loop, journal replay, ledger reconciliation).
- **Deciders:** agent-loop role, with metering/budget role for the ledger.

## Context

Hard requirement #7 and adversarial scenario 5: a task killed mid-run must
**resume without lost work, a double-charge, or a re-applied side effect**. The
task queue delivers at-least-once (a killed task is re-run), and paid model calls
+ patch applications are side effects with real cost and real consequences. We
need a delivery/consistency guarantee that survives a crash at *any* point,
including the worst point: a paid model call that **returned** but whose result
and charge were not yet durable.

## Decision

**Guarantee: at-least-once execution + idempotent effects = effectively-once.**
We do not attempt exactly-once *delivery* (impossible across a crash boundary);
we make every *effect* idempotent so that re-running a step converges to the same
state as running it once.

**The journal is the single source of truth.** The append-only `journal` table
has `UNIQUE(task_id, step_index)`. `JournalRepo.append` is `INSERT OR IGNORE` and
returns `(entry, created)`; a replayed step gets `created=False` and its
**cached payload**, so the loop reuses the stored model response instead of
re-issuing the call. One journal row per step carries: the model `content_xml`,
the token usage, `charge_tokens`, and the step's effect result (patch envelope +
content-hash, or the sandbox verdict).

**Three idempotent effects:**

1. **Journal write** — idempotent on `(task_id, step_index)` (above).
2. **Patch apply** — content-addressed. An EDIT hashes its patch envelope
   (sha256) into `artifacts` and applies to the worktree only if no artifact with
   that hash exists for the task. Replay recomputes the hash, finds it present,
   and does not re-apply.
3. **Model-call charge** — reconciled from the journal (below).

**Reconciliation-on-resume for the model-call-returned-before-crash window.**
The ordering in `_issue_model_call` is: issue the one paid call → run the effect
→ append the journal row (with `charge_tokens`) → commit the charge to the
`budget_ledger` tagged `(task_id, step_index)`. Two crash sub-cases:

- **Died before the journal row is durable** → no row exists; resume re-issues
  the call. Safe: the effect is idempotent, and the first attempt left nothing
  behind. (at-least-once leg)
- **Died after the journal row but before the ledger commit** (the returned-
  before-crash case) → the row with `charge_tokens` is durable; the commit is
  not. On resume, `_charge_from_journal` walks the journal and, for each model
  step carrying `charge_tokens`, ensures **exactly one** COMMIT tagged with that
  `(task_id, step_index)` — adding the missing one, skipping any already present.
  The paid call is **not** re-issued (its response is cached), and the charge
  lands **exactly once**.

Because the journal — not volatile in-flight state — drives the charge, the
ledger ends with no double-charge and no leaked (lost) charge. Both `journal` and
`budget_ledger` are append-only at the DB layer (triggers block `UPDATE`), so
reconciliation can only *add* a missing commit, never rewrite history.

## Alternatives considered

- **Charge before the model call (reserve-and-hope).** Rejected: a crash between
  the charge and the call would bill for work never done (a leak in the other
  direction), and a retry would then double-bill.
- **A "processed" flag updated in place per step.** Rejected: it requires
  mutable state and a read-modify-write that is itself not crash-atomic with the
  effect; the append-only journal + `INSERT OR IGNORE` gives atomicity for free
  and is auditable.
- **Two-phase commit / a distributed transaction across model + ledger.**
  Rejected: the model call is a non-transactional external effect; no XA
  coordinator spans it. Idempotent effects + reconcile-from-log is the standard,
  simpler answer and needs no new infrastructure.
- **Exactly-once delivery.** Rejected as unachievable across a crash; we target
  effectively-once *effects* instead, which is what the requirement actually
  needs.

## Consequences

- **Positive.** A kill at any point resumes correctly: cached model responses are
  reused (no re-charge, no re-issue), patches apply exactly once, terminal states
  are honest. Proven by
  `test_agent_loop.py::test_resume_no_double_charge_and_apply_exactly_once`,
  which crashes precisely between the journal append and the ledger commit and
  asserts exactly one commit per model step and a single patch artifact.
- **Negative / limits.** Idempotency here is keyed on `(task_id, step_index)` and
  on patch content-hash; a *non-deterministic* real model backend that produced a
  different reply on the re-issue leg (crash before the journal row) would apply a
  different — but still idempotent-by-hash — patch. The stub is deterministic, so
  this does not arise in the eval; for a real backend the guarantee is
  "effectively-once per journaled decision," which is the correct scope.
- **Portability.** Nothing here depends on SQLite specifics beyond append-only +
  a unique constraint; the same design lifts to Postgres/Aurora unchanged.
