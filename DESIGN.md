# DESIGN — Agentic Code-Intelligence Platform (ACP)

A multi-user service where an autonomous agent navigates a large, polyglot
codebase and ships **verified** changes — safely, concurrently, within a hard
budget. The retrieval model, agent control loop, isolation model, and sandbox are
hand-owned; no framework hides the core loop. This document follows the altitude
order requirements → estimates → HLD → LLD → failures → tradeoffs → framing →
conclusion. Per-decision depth lives in [`docs/adr/`](docs/adr/); this doc links
rather than inlines it to stay within the page budget.

---

## 1. Requirements

**Functional (assignment §2):** ingest a repo into a per-user workspace and build a
structural, cross-language index; expose budgeted retrieval primitives; run a
hand-written plan→retrieve→edit→verify→repair loop; verify changes only inside an
isolated sandbox; isolate users and concurrent tasks; resist prompt injection and
keep secrets out of prompts/outputs; meter and enforce budgets; be operable.

**Non-functional:** deterministic keyless stub mode (reproducible eval); one-command
bring-up; effectively-once execution under crash; default-deny sandbox egress;
honest terminal states; portable state layer (SQLite→Postgres).

### 1.1 Coverage matrix — every assignment ask → mechanism → proving artifact

**Hard requirements (§3):**

| # | Requirement | Mechanism | Proof (exists) |
|---|---|---|---|
| 1 | API-only access | Only `gateway` binds a port; other modules in-process; sandbox on private net | `tests/integration/test_api_boundary.py::test_route_inventory_no_index_or_sandbox_exposure`, `::test_no_route_for_raw_index` |
| 2 | User isolation by construction | Accessors keyed on `user_id`; identity from key not body; NotFound (not 403) for foreign IDs | `test_api_auth.py::test_user_id_derived_from_key_not_body`, `test_api_tasks.py::test_post_tasks_isolation_wrong_workspace`, `test_worktree_isolation.py` |
| 3 | Auth on every endpoint | Hashed keys (`<prefix>.<secret>`), `hmac.compare_digest`, per-key rate limit → 429 | `test_api_auth.py`, `test_api_boundary.py::test_v1_tasks_endpoint_requires_auth`, `test_rate_limit.py` |
| 4 | Real verification | `VerificationResult.verified = applied ∧ built ∧ tests_passed ∧ no-kill`, computed in Docker | `make eval-docker` (5/5), `test_sandbox_verification.py`, `test_agent_loop_docker.py` |
| 5 | Injection-resistant + secret hygiene | XML instruction/data channels, closed action allowlist, retrieval-boundary redaction | `test_agent_loop.py::test_injection_has_zero_effect_differential`, `test_retrieval_redaction.py`, `make redteam` |
| 6 | Server-side budgets | Pre-op ledger check on token + step + wall-clock; clean checkpoint stop | `test_api_tasks.py::test_budget_exhausted_stops_cleanly`, `test_wall_clock_budget.py::test_wall_clock_budget_stops_clean_with_partial_progress` |
| 7 | Correctness under failure | Append-only journal `UNIQUE(task_id, step_index)` + idempotent effects = effectively-once | `test_agent_loop.py::test_resume_no_double_charge_and_apply_exactly_once`, `test_regression.py::test_reg05_resume_no_double_charge_apply_once` |
| 8 | One-command local run | `make up` → keyless stub mode; `docker-compose.yml` | `README` Quickstart; `make health` → 200; `tests/smoke/test_health.py` |
| 9 | Operable | `/healthz` `/readyz` `/metrics`, JSON logs, journal = trace | `tests/smoke/test_health.py`, `test_readyz_repoint.py`, `make trace` |

**Adversarial scenarios (§2.8 box, the 8 the spec plants):**

| # | Scenario | Mechanism | Proof (exists) |
|---|---|---|---|
| 1 | Repo exceeds context window | Structural index + span-level budgeted retrieval; never dump files | `tests/unit/test_index.py`, `test_retrieval_service.py` |
| 2 | Multi-file change across call sites | Index-driven whole-identifier rename at every reference in one verified step | `test_multifile_rename.py::test_rename_completeness_against_index_reference_set` |
| 3 | First patch fails → repair | Real captured sandbox stderr fed via DATA channel; bounded repair loop | `test_self_repair.py::test_repair_input_contains_actual_captured_error_text` |
| 4 | Adversarial instruction in a file | Retrieved code rides `<data>`, never `<instruction>`; closed allowlist | `test_agent_loop.py::test_injection_has_zero_effect_differential` (byte-identical journal) |
| 5 | Killed mid-task → resume | Journal replay: cached model calls, idempotent patch apply, no double-charge | `test_agent_loop.py::test_resume_no_double_charge_and_apply_exactly_once` |
| 6 | Concurrent writes to one workspace | Advisory lock + base-commit check → one commits, other rejected | `test_apply_commit_concurrency.py`, `test_concurrency.py` |
| 7 | Budget exhausted mid-task | Pre-op gate stops at journaled checkpoint, partial reported, workspace intact | `test_wall_clock_budget.py`, `test_self_repair.py::test_budget_exhausted_during_repair_and_resumes_without_recharge` |
| 8 | Build/test phones home or runs forever | `--network=none` (fails closed) + wall-clock `proc.kill()` | `test_sandbox_verification.py` (network + deadline cases) |

Every row cites a file confirmed present in the tree. The §2.8 design-only asks
(org/tenant, review surface, horizontal scale) are addressed as design in §6.

---

## 2. Estimates to run flawlessly

**Prerequisites.** Docker + Docker Compose (only for the sandbox tier and
`make up`), Python 3.12, Poetry 2.x. Nothing else; keyless by default.

**Resource footprint.**
- *Control plane*: one Python process (gateway + in-process modules) + one SQLite
  file in WAL mode. Idle footprint is small (tens of MB); it holds no untrusted
  code.
- *Per-task sandbox container* (defaults in `Settings`, enforced by `docker run`
  flags): CPU `--cpus` (cgroup), memory `--memory`+`--memory-swap` equal (swap off,
  default 200–512 MB class), disk = a size-capped `--tmpfs /work` on a
  `--read-only` rootfs, wall-clock kill (default 60 s). One container per
  verification, torn down explicitly (named container + `docker rm -f`).

**Cost model — back of envelope.** Model calls dominate; then re-verification
(each repair adds a full sandbox build+test); then re-indexing (cheap due to the
incremental-equivalence invariant — only the edited file re-parses).

- *Stub backend (keyless):* **$0/task** — no model calls. This is the eval path, so
  the entire eval harness runs at zero marginal cost.
- *Real model backend:* `cost_usd = (tokens_in/1000)·price_in + (tokens_out/1000)·price_out`.
  Placeholder constants in `Settings`: `price_per_1k_input=0.003`,
  `price_per_1k_output=0.015` (**PLACEHOLDER — Claude Sonnet list price ~2026-01;
  verify before quoting**). A single-file task at ~30k in / ~4k out ≈
  **$0.15/task**; `$/verified-change` ≈ `$/task ÷ verify-rate`, so a first-patch
  failure that triggers one repair roughly doubles it.
- *What bends the curve:* structural retrieval charges the agent for *symbols an
  edit touches*, not file bulk (a span always costs strictly fewer tokens than the
  file — `test_retrieval_redaction.py::test_token_cost_is_monotonic_in_bytes`), so
  prompt size stays bounded even on an over-context repo. Deliberate caching points:
  the index is deterministic and reused across a run; retrieval results are pure
  functions of `(snapshot, query)` and replayable; verification results are
  journaled and not recomputed on resume.

---

## 3. HLD then LLD

### 3.1 High-level design

ACP is a **modular monolith** (bounded modules in one control-plane process) paired
with a **separate Docker sandbox tier** for untrusted execution.

**Three invariants define the system:**
1. **The API is the only door.** Only `gateway` binds a port; every other module is
   an in-process library, and the sandbox tier has no host-published port. "Index/
   sandbox/model unreachable by consumers" holds *by construction*, not policy.
2. **The sandbox is the oracle.** A change is *verified* only when the sandbox
   returns `applied ∧ built ∧ tests_passed`. No model self-report can produce
   success.
3. **The journal is the source of truth.** The append-only journal is both the
   per-run trace and the replay substrate; charges and effects reconcile from it.

**Request lifecycle:**

```
 consumer ──Bearer key──▶ gateway (only port)
                             │  auth → derive user_id → create task row
                             ▼
                        orchestrator (hand-written loop)
        ┌──────────────── PLAN → RETRIEVE → EDIT → VERIFY ───────────────┐
        │                    │         │        │       │                 │
        │              model_gateway  retrieval workspace sandbox_client  │
        │              (holds key,    (budgeted  (per-task  (Docker:      │
        │               XML I/O)       primitives) worktree) network=none)│
        │                                                  │              │
        │                          verify fails ──▶ REPAIR ┘ (feed real   │
        │                                                     stderr via  │
        │                                                     <data>)     │
        └──▶ { verified_success | gave_up | budget_exhausted }           │
                             │
                             ▼   every step: 1 journal row + 1 ledger commit
                        SQLite (WAL): users·api_keys(hashed)·tasks·
                        journal(append-only)·budget_ledger·workspaces·artifacts
```

Control plane and sandbox are separate tiers: the gateway holds no Docker socket
under bare compose — that socketless boundary is the isolation guarantee, and it is
why `/readyz` honestly reports `sandbox:false` there (real verification is
host-managed via `make eval-docker`).

### 3.2 Low-level design (per component)

Each component leads with one plain-language sentence (what it does and why it
matters) a non-technical reader can follow, then the technical detail:
module · interface · proving test.

- **gateway / auth** — *The single front door: it checks who you are and turns your
  request into an owned task, so nothing untrusted ever addresses the internals
  directly.* `src/acp/gateway/` (`create_app`, `auth.py::require_auth`). Bearer
  `<prefix>.<secret>`, SHA-256 stored, constant-time compare, sliding-window rate
  limit → 429. `user_id` comes only from the key, never the body.
  *Proof:* `test_api_auth.py`, `test_api_boundary.py`, `test_rate_limit.py`.
- **orchestrator / loop** — *The agent's brain: an explicit, hand-written sequence
  of think→look→edit→test→fix that you can single-step, so we own and can defend
  every decision.* `src/acp/orchestrator/loop.py` (`AgentLoop.run`). State machine
  with per-step budget gate, one model turn, allowlist parse, effect, one journal
  row, one ledger commit. Three terminal states only.
  *Proof:* `test_agent_loop.py`, `test_agent_loop_docker.py`, `test_self_repair.py`.
- **retrieval** — *How the agent reads the codebase without drowning in it: it pays
  budget per symbol it touches, so a repo far bigger than the model's context stays
  affordable.* `src/acp/retrieval/service.py` (`RetrievalServiceImpl`). Allow-listed
  primitives (`search_symbols`, `definition`, `references`, `read_span`, `read_file`,
  `list_dir`, `structural_grep`); cost = pure function of returned post-redaction
  bytes; atomic ledger charge (`charge_atomic`, single `BEGIN IMMEDIATE`).
  *Proof:* `test_retrieval_service.py`, `test_retrieval_budget_toctou.py`.
- **index** — *A structural map of the code (symbols, call sites, imports) instead
  of text chunks, so "find every caller of X" is exact — which is what makes
  correct multi-file edits possible.* `src/acp/index/`. Tree-sitter over Python +
  TypeScript; per-file partitions; canonical byte-stable serialization; incremental
  update equals full rebuild.
  *Proof:* `test_index.py::test_incremental_reindex_equals_full_rebuild`.
- **workspace / worktree** — *Each user and each task gets its own private copy of
  the repo, so two runs can't corrupt each other or peek at each other's edits.*
  `src/acp/workspace/service.py`. Path keyed on `(user_id, workspace_id)`; NotFound
  before disk access; zip-slip/tar-escape guarded; `commit_worktree` = advisory lock
  + base-commit check → serialize or reject.
  *Proof:* `test_workspace_index.py`, `test_apply_commit_concurrency.py`.
- **sandbox_client** — *The truth-teller: it actually applies the patch, builds, and
  runs the repo's tests inside a locked-down container — the only thing allowed to
  declare a change "done".* `src/acp/sandbox_client/docker_runner.py`. `apply→build
  →test`; `--network=none`, cgroups, `--cap-drop ALL`, non-root, read-only rootfs,
  wall-clock kill; kills are structured results (`KilledReason`), never hangs.
  *Proof:* `test_sandbox_verification.py`, `test_polyglot_parity.py`.
- **model_gateway** — *The only place the model API key lives, and the only place
  that talks to the model — using strict XML so untrusted code can never smuggle in
  a command.* `src/acp/model_gateway/` (`prompt.py::build_channels`, `parse_action`).
  Separate `<instruction>`/`<data>` channels; closed action vocabulary; typed
  upstream errors (`UpstreamModelError`/`ModelRefusal`/`ModelTruncated`), no key in
  any error.
  *Proof:* `test_model_gateway.py`, `test_claude_backend.py`.
- **db / journal + ledger** — *The tamper-proof record of what happened and what was
  spent, so a crashed run resumes exactly once without losing or repeating work.*
  `src/acp/db/`. SQLite WAL; append-only journal + ledger (triggers block UPDATE);
  `charge_atomic` serializes budget.
  *Proof:* `test_data_access.py`, `test_db_thread_safety.py`, `test_regression.py`.
- **dashboard** — *An operations view that reads only the public API — its existence
  proves the API-only boundary holds, because a full UI needs no backdoor.*
  `GET /dashboard` (static) + `GET /v1/dashboard/*` (auth-scoped). No privileged
  access; no browser storage (multi-user surface must be server-fed).
  *Proof:* `test_api_boundary.py::test_dashboard_data_endpoints_not_privileged`,
  `::test_dashboard_summary_cross_user_isolation`.

---

## 4. Failure modes & mitigations

| Failure mode | Trigger | Detected | Mitigation in this project | Residual risk | Proof |
|---|---|---|---|---|---|
| Repo exceeds context | Large repo | N/A (default assumption) | Structural index + span retrieval; pay per symbol | Very large index re-serialization (seam, ADR-0001) | `test_index.py`, `test_retrieval_service.py` |
| Multi-file change | Rename across call sites | Index reference set | Whole-identifier rewrite at every ref, one verified step | Same-name distinct symbols in one lang (name-scoped) | `test_multifile_rename.py`, `test_rename_collision.py` |
| First patch fails tests | Bad edit | Sandbox `verified=false` + real stderr | Bounded repair loop; real failure via `<data>` | Unfixable → `gave_up` (honest) | `test_self_repair.py` |
| Adversarial instruction in file | Injected docstring/comment | Structural — never reaches `<instruction>` | Channel separation + closed allowlist; not indexed as ref | A live model could still be *worse* than stub; boundary still holds | `test_agent_loop.py::test_injection_has_zero_effect_differential` |
| Killed mid-task | Process death | Missing journal row / ledger commit | Replay: cache model calls, idempotent apply, reconcile charge | Crash *between* journal + ledger handled by `_charge_from_journal` | `test_agent_loop.py::test_resume_no_double_charge_and_apply_exactly_once` |
| Concurrent writes | Two tasks, one workspace | Base-commit mismatch under lock | Serialize or reject (`ConflictError`); never clobber | Advisory lock is per-process (monolith); prod → shared lock | `test_apply_commit_concurrency.py`, `test_concurrency.py` |
| Budget exhausted (token/wall/step) | Ceiling breached | Pre-op gate | Stop at journaled checkpoint, partial report, workspace intact | USD stop is token-denominated pending real pricing | `test_wall_clock_budget.py`, `test_api_tasks.py::test_budget_exhausted_stops_cleanly` |
| Build/test phones home or hangs | Malicious/broken code | `--network=none`; wall-clock kill | Egress fails closed → `network`; deadline → `deadline` | CPU throttle configured, not timing-proven (honest) | `test_sandbox_verification.py` |
| Model outage / refusal / truncation | Provider error | `stop_reason` / SDK exception | Typed `UpstreamModelError`/`ModelRefusal`/`ModelTruncated` (502) | Depends on provider surfacing correct `stop_reason` | `test_claude_backend.py` |
| SQLite writer contention | Concurrent commits | `BEGIN IMMEDIATE` lock | Single-writer serialization point; per-thread connections | Single-writer ceiling → Postgres for scale (ADR-0000) | `test_retrieval_budget_toctou.py`, `test_db_thread_safety.py` |
| Corrupted / drifted index | Head moves under a query | `SnapshotRef` mismatch | Refuse to answer from a drifted snapshot | Rebuild cost on very large repos | `test_retrieval_service.py` |
| Zip/tar-slip on ingest | Malicious archive | Path containment check | Extraction guarded; `IsolationViolation` | — | `test_workspace_index.py` |
| Repo with no tests | Missing suite | `pytest` exit / no collection | Verify returns not-passed (no false success) | Assumes repo *has* verifiable tests (documented assumption) | `test_sandbox_verification.py` |

---

## 5. Tradeoffs

Every (−) is pulled from an ADR. This table lists **every** tradeoff taken.

| Decision | Gain | Lose | When the cost bites | Mitigation / exit |
|---|---|---|---|---|
| Modular monolith (ADR-0000) | Fewer failure surfaces, one artifact, clean seams | Shared process blast radius | High horizontal scale | Split at interface seams; the one dangerous workload is already out in the sandbox |
| SQLite single-writer WAL (ADR-0000) | Zero-dep, portable, safe concurrent writes | One writer at a time | Write-heavy multi-node | Same repo interface → Postgres/Aurora |
| Structural index, name+lang-scoped (ADR-0001) | Exact call graph, cheap, deterministic | No type inference; same-name collision | Precision-critical rename of ambiguous names | Sandbox is the semantic backstop; LSP/scope-aware resolver later |
| Index as single JSON (ADR-0001) | Determinism + incremental-equivalence cheaply | O(files) recompute, monolithic load | Very large repos | Per-file on-disk partition store behind same interface |
| Docker shared-kernel sandbox (ADR-0003) | Ubiquitous, real limits, one artifact | Weaker than a VM boundary | Hostile kernel-exploit code | gVisor/Firecracker documented production path |
| Whole-identifier rename (ADR-0004/loop) | Collision-safe vs substring; no false rewrites | Textual within in-scope files, not full AST bind | Two distinct same-name symbols in one file | Tree-sitter byte-precise edit ranges (deferred) |
| Whole-file JSON patch envelope | Tool-free, deterministic, runner-portable | Coarser than a line diff | Huge files, tiny edits | Span/patch ops already modeled; finer diffs later |
| API keys vs JWT/mTLS (ADR-0002) | Simple, revocable, no PKI | No federation/rotation infra | Cross-service / enterprise SSO | mTLS + rotation documented as next step |
| Effectively-once "per journaled decision" | Crash-safe resume, no double-charge | Guarantee is per journaled step, not sub-step | Non-idempotent external side effect | All effects made idempotent by design |
| Stub-model determinism | Keyless reproducible eval | Doesn't exercise real-model nondeterminism | Behavior only a live model shows | `MODEL_BACKEND=claude` runs the same loop against real Claude |
| Placeholder pricing | Cost model without a live account | $/task not authoritative | Any real dollar quote | Confirm price → one-line constant change |

---

## 6. Pattern / Accelerator / Product feedback

**Pattern (repeatable engineering the project uses).** Oracle-gated phases (no
phase merges until its programmatic oracle is green); sandbox-as-contract (one
verification contract, multiple conforming runners); the journal/effectively-once
substrate (append-only log = trace + replay); budgeted retrieval primitives (cost
is a pure function of returned bytes); Makefile-as-thin-interface (every target a
one-line delegate to `agentctl`/compose, logic stays testable); stub-model
determinism for keyless reproducibility.

**Accelerator (reusable leverage for the next engagement).** The
eval-harness+oracle template drops in held-out tasks with no code change; the
config-driven `SANDBOX_RUNNER` / `MODEL_BACKEND` swaps let a new team change one
env var to retarget the sandbox language or model. **Would-build (not in the repo,
marked honestly):** IaC/Terraform for the sandbox pool, a CI/CD pipeline gating
`test → eval-docker → coverage`, and a config-driven client-onboarding workflow —
none of these exist today; they are the natural next leverage, not a shipped claim.

**Product feedback (real problems solved + ambiguities surfaced).** Solves:
verified-only changes (no fabricated success), isolation between users/tasks, hard
budget ceilings, and injection defense — the four things that make an autonomous
agent trustworthy across many users. Surfaced ambiguities: the platform assumes the
target repo *has* runnable tests (no tests ⇒ nothing to verify against); a real
`$/task` needs a confirmed model price; and org/tenant scope (§2.8) is design-only,
so quota/tenant-isolation semantics need a product decision before build.

---

## 7. Conclusion

The thesis: an agent you can trust across many users ships **verified-only** changes
through a **hand-owned** loop, on **isolated** per-task worktrees, under **hard
budgets**, behind an **injection-resistant** trust boundary — and every one of
those claims is backed by a programmatic oracle, never a self-report. What was cut
is stated plainly: per-language sandbox *verification* (the polyglot runners are a
language-diverse harness with a Python verification core), org/tenant isolation and
the review UI (design-only per §2.8), a live-model $/task (placeholder pricing), and
CI/CD/IaC (would-build). What comes next, in order: confirm pricing and wire the USD
stop, add `org_id` to the scoped accessor, broaden index language support, and stand
up the eval-gated CI pipeline. **One-line takeaway:** nothing is "done" until the
sandbox says so — and the whole platform is built so that rule cannot be bypassed.

---

### Appendix — decision records & schema

Per-decision depth: [ADR-0000](docs/adr/0000-modular-monolith-and-sqlite-wal.md)
(monolith + SQLite), [0001](docs/adr/0001-retrieval-model.md) (retrieval),
[0002](docs/adr/0002-isolation-mechanism.md) (isolation),
[0003](docs/adr/0003-sandbox-technology.md) (sandbox + polyglot parity),
[0004](docs/adr/0004-delivery-consistency-guarantee.md) (effectively-once),
[0005](docs/adr/0005-makefile-as-interface.md) (Makefile),
[0006](docs/adr/0006-eval-before-merge.md) (eval-before-merge, the "setting the bar"
standard). State schema DDL: [`src/acp/db/schema.sql`](src/acp/db/schema.sql)
(`users`, `api_keys` hashed-only, `workspaces`, `tasks`, `journal` append-only
`UNIQUE(task_id, step_index)`, `budget_ledger` append-only, `artifacts`;
append-only enforced by DB triggers).
