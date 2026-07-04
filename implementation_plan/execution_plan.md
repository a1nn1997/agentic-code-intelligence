# Agentic Code-Intelligence Platform — Execution Plan

**Purpose:** This is the master reference and source of truth for building the Sarvam Gen AI take-home: a multi-user service where an autonomous agent navigates a large, polyglot codebase and ships *verified* changes safely, concurrently, and within a hard budget. It captures every requirement, every locked decision, the phase-by-phase build order, and — critically — the documentation deliverables and how each phase produces them.

**Working principle:** Depth over breadth. One module at a time. Each phase lands with tests + a programmatic oracle before the next begins. The retrieval model, agent control loop, isolation model, and sandbox are hand-owned — no framework hides the core loop.

---

## 1. Locked Decisions (do not re-litigate)

| Area | Decision | Rationale |
|---|---|---|
| **Language** | Python 3.12 everywhere in the control plane and sandbox-orchestration | `py-tree-sitter` is mature and GC-safe; Go bindings require manual `Close()` on C-FFI objects (leak/double-free risk under concurrent incremental re-index); uniformity eliminates a class of first-run FFI bugs. First-go correctness > cred. |
| **Dependency mgmt** | Poetry with committed `poetry.lock`; hermetic Docker builds (`poetry install --no-root --only main`) | Deterministic resolution backs the "runs first go" priority. |
| **Architecture** | Modular monolith (bounded modules, one process) + separated Docker sandbox tier | Fewer failure surfaces than N network services; clean boundaries allow later split. Described honestly in DESIGN.md with the tradeoff defended. |
| **State** | SQLite in **WAL mode** with `BEGIN IMMEDIATE`; ledger is the serialization point | Portability claim (works on SQLite ⇒ works on Postgres/Aurora); WAL + IMMEDIATE gives safe concurrent writes for journal/ledger. |
| **Task queue** | SQLite-backed with row locking + visibility timeout | At-least-once delivery, crash-resume for free, one fewer dependency than Redis/Celery. |
| **Vector layer** | Chroma **in-memory** (optional semantic complement, never a replacement for structural) | Structural retrieval is primary; semantic is additive. |
| **Sandbox** | Docker only, `--network=none` + cgroup CPU/mem/disk + wall-clock kill + dropped caps + non-root + read-only rootfs + tmpfs | The single most important requirement. This is the ONE place Docker is mandatory. |
| **Model brain** | Claude Sonnet via Anthropic API behind a **Model Gateway**; deterministic **XML stub** for keyless eval | API key lives only in the gateway, never in a client or sandbox path. |
| **LLM I/O format** | Strict XML for all prompts and parses | XML-delimited channels make the instruction/data trust boundary *structural*, not prose. |
| **Indexed languages** | Python + TypeScript (satisfies polyglot at the index level, where the spec demands it) | We own the index; the model never touches a parser. |
| **Stretch priority** | Self-repair from real test output first, then multi-file rename | Both targeted; self-repair prioritized. |
| **Polyglot sandbox CLIs** | Go / Rust / TypeScript runners — **Phase 8, strictly last**, gated behind all core phases green | Same sandbox contract, parity-tested against the Python reference runner. Strongest "own & swap the core" signal; never blocks a pass/fail requirement. |
| **Auth** | Hashed API keys (prefix + secret, constant-time compare, per-key rate limit + budget) | Simple, defensible; mTLS documented as production hardening. |
| **Operator interface** | `Makefile` is the single command **interface**; every target is a one-line delegate to a Python CLI (`agentctl`), a `scripts/` entry, or compose — **never** a second codebase of shell logic | One universal verb (`make <thing>`) for operators/evaluators, while behavior stays in real, unit-testable code. `make help` is the default target, auto-generated from `##` target comments (self-documenting). All targets `.PHONY`. Serves README commands + walkthrough + regression from one surface. |
| **Observability dashboard** | Static vanilla HTML/CSS/JS bundle (dark KPI-card + timeline + filterable-table layout), served by the gateway, that reads **only** through the API + structured logs/journal — **no privileged access** to index/sandbox/model. `make logs` opens it: snapshot by default, `LIVE=1` for auto-refresh polling | It is a consumer like any other, which *proves* the API-only boundary holds (a full operability UI needs no backdoor). Renders data we already emit (metering, `/metrics`, journal via events) — a renderer, not a new data plane. |

---

## 2. Hard Requirements → Mechanism → Owning Phase

These are **pass/fail**. Every one maps to a concrete mechanism and the phase that lands it. This table is our defense cheat-sheet.

| # | Hard Requirement | Mechanism | Phase |
|---|---|---|---|
| 1 | **API-only**; index/sandbox/model unreachable by consumers | Internal modules publish no ports; only the gateway exposes one; private Docker network | 0, 6 |
| 2 | **User isolation by construction**, server-side on every read/write | Scoped accessor keyed on `(user_id, workspace_id)` that *cannot address* another user's data; RLS-style checks as defense-in-depth | 1, 6 |
| 3 | **Auth on every endpoint** | Hashed API keys, constant-time compare, per-key rate limit | 6 |
| 4 | **Real verification** — done only if applied + built + tests passed in sandbox | Sandbox result is the oracle; never a model self-report | 3, 4 |
| 5 | **Injection-resistant**; secrets never enter prompts/outputs | XML instruction/data trust boundary + fixed tool allowlist + secret redaction at retrieval boundary + egress-deny backstop | 2, 3, 4 |
| 6 | **Budgets enforced server-side**; clean stop | Atomic BudgetLedger, pre-op check (token estimate → reconcile), wall-clock deadline, checkpoint + partial report on breach | 4, 6 |
| 7 | **Correctness under failure** — resume without lost work, double-charge, or re-applied side effects | Append-only journal, `(task_id, step_index)` unique + idempotency keys; replay returns cached model responses; patches gated on applied-flag/content hash. **Guarantee: at-least-once execution + idempotent effects = effectively-once** | 4 |
| 8 | **Runs locally with one command** | `make up` / `docker-compose up` boots services + SQLite + sandbox host + seeded sample repo + stub model, zero keys | 0 |
| 9 | **Operable** — health/readiness, structured logs, metrics, per-run traces | `/healthz` `/readyz`, JSON logs correlated by `task_id`/`step`, `/metrics`; the journal *is* the per-run trace; SLOs + runbook in DESIGN.md | 0, 7 |

---

## 3. Adversarial Scenarios → How We Survive Them

The spec plants these deliberately; they are the assignment, not edge cases.

| Scenario | Defense | Phase |
|---|---|---|
| 1. Repo does not fit in context (default case) | Structural index + budgeted span-level retrieval; never dump files into the model | 1, 2 |
| 2. Multi-file change across many call sites | References/call-graph query drives span patches at every site + test | 5 |
| 3. First patch fails tests → repair | Loop reads *real* sandbox failure output, re-plans within budget | 5 |
| 4. Adversarial instruction inside a file | XML data-channel labeling; model acts only via tool allowlist; retrieved content can never become a command | 2, 4 |
| 5. Process killed mid-task → resume | Journal replay: completed model calls cached, patches gated on applied-flag/hash; no double-charge, no re-apply | 4 |
| 6. Two tasks edit same workspace concurrently | Per-task isolated worktree from a bare mirror; commit guarded by advisory lock + base-commit check → rebase-reverify or reject; never silent clobber | 4 |
| 7. Budget exhausted mid-task | Pre-op ledger check → checkpoint at consistent boundary → partial progress reported | 4, 6 |
| 8. Build/test phones home or runs forever | `--network=none` (fail closed) + wall-clock kill | 3 |

---

## 4. Documentation Deliverables — Core Expectations

**Docs carry equal weight to the system.** A brilliant system with a thin write-up is graded incomplete. Below is every doc deliverable, what "good" means for it, and which phase produces it.

### 4.1 `DESIGN.md` (2–5 pages)
The centerpiece. Must contain, each defended with reasoning:
- **Architecture & data flow** with a diagram (the dashed-boundary picture: everything inside owned & unreachable, API the only door).
- **Retrieval model and why it beats naive RAG here** — structural, budget-aware navigation vs. embeddings-over-chunks with no symbol awareness. Explicitly explain why chunk-and-stuff collapses on a repo that exceeds the context window.
- **Agent loop**, its termination (three terminal states only), and how runs are made **replayable** (journal design).
- **User-isolation model** and exactly how it extends to org/tenant — *and where it would break* (§2.8 is design-only; we must still write it).
- **Prompt-injection trust boundary** — how instructions (authenticated user) are separated from data (retrieved code); how retrieved content cannot escalate to commands.
- **Sandbox & egress model** — isolation tech, resource limits, default-deny networking.
- **Metering & $/task reasoning** — $/task and $/verified-change back-of-envelope; what drives the cost curve (model calls dominate; re-verification and re-indexing next); where caching bends it.
- **SLOs, capacity posture, on-call/runbook** for the top two failure modes.
- **What we cut and would do next** — honest scope statement.

> **Produced across all phases; assembled and finalized in Phase 7.** Each phase writes its DESIGN.md section as it lands, so the doc is never a last-minute scramble.

### 4.2 Architecture Decision Records — `docs/adr/`
One ADR per hard decision the spec explicitly names. Minimum four:
1. **Retrieval model** — structural index + budgeted primitives; why not embeddings-only.
2. **Isolation mechanism** — per-task worktree / COW; scoped accessor; SQLite WAL + IMMEDIATE as serialization point.
3. **Sandbox technology** — Docker baseline (network=none, cgroups, caps drop); gVisor/Firecracker as documented production path; the sandbox-as-contract framing with four conforming runners.
4. **Delivery / consistency guarantee** — at-least-once + idempotent effects = effectively-once; journal + ledger design.

Each ADR: context → decision → alternatives considered → consequences/tradeoffs.

> **Written in the phase where the decision is made** (retrieval → Phase 2, isolation → Phases 1/4, sandbox → Phase 3, delivery → Phase 4). Never backfilled.

### 4.3 "Setting the Bar" Note (½ page)
One opinionated engineering standard we'd insist on from day one. **Recommended pick: eval-before-merge** — it mirrors the product's own thesis that nothing is "done" until verified. Must be opinionated and defensible, not a survey.

> **Produced in Phase 7.**

### 4.4 `README.md`
Exact bring-up commands + example API calls (curl / .http / Postman). Must let an evaluator run everything keyless.

> **Started in Phase 0, kept current every phase, finalized in Phase 7.**

### 4.5 Eval Harness — `eval/`
≥5 reproducible tasks against the sample repo, each with a **programmatic oracle** (did tests pass? did the right call sites change?), runnable in stub mode. Must include ≥1 planted prompt-injection that is defended and ≥1 secret-exfil attempt that fails. Designed so held-out tasks/repos can be dropped in.

> **Produced in Phase 7**, but the sample repo it runs against is designed in Phase 1.

### 4.6 Test Suite
Full pyramid: **unit, integration, e2e, smoke, regression.** At least one integration test exercises index → task → verified change end-to-end.

> **Each phase ships its own tests; the pyramid is completed and the cross-cutting regression/e2e suite lands in Phase 7.**

### 4.7 Git Discipline
Incremental, meaningful commits; **branch per capability/phase**; no single squashed dump. An ADR per hard decision. The Git history is itself evaluated as a signal of how we work.

> **Enforced every phase.** Phase does not merge until its oracle is green.

### 4.8 Walkthrough (5–10 min, recorded or live)
Happy path + one failure/recovery (crash-resume or budget-exhaustion) + one defended prompt-injection.

> **Scripted in Phase 7** from the eval tasks.

---

## 5. Operator Interface — Makefile Command Surface

**Framing (locked):** The `Makefile` is the single command *interface*, not the source of logic. Every target is a thin one-line delegate to a real, testable entrypoint — a Python CLI (`agentctl`, built with Typer/Click), a script under `scripts/`, or a `docker compose` invocation. Make is the menu; the kitchen is elsewhere. This keeps `make <thing>` as the universal operator verb while behavior stays under test coverage. `make` with no argument prints an auto-generated help menu built from `##` comments; every target is `.PHONY`.

This one surface serves three deliverables at once: the README's "exact commands" section mirrors `make help`; the walkthrough is literally `make demo-*` + `make redteam`; and the demo targets double as regression checks.

**Lifecycle**
- `make up` — boot the entire stack in **stub mode, keyless** (this IS hard-requirement #8)
- `make up-claude` — same, Model Gateway → Claude (needs key in `.env`)
- `make down` — tear down, preserve volumes
- `make clean` — tear down + wipe volumes/artifacts (fresh state)
- `make logs` — open the **observability dashboard** (static HTML served by gateway; snapshot by default). Also tails raw structured logs.
- `make logs LIVE=1` — dashboard in live mode (auto-refresh, polls `/metrics` + events while tasks run)
- `make ps` / `make health` — service status + hit `/healthz` `/readyz`

**Setup**
- `make install` — `poetry install` + build sandbox image
- `make migrate` — apply SQLite schema/migrations (WAL)
- `make seed` — load synthetic Python+TS sample repo + demo user/API key

**Develop**
- `make fmt` / `make lint` / `make typecheck` — formatting, ruff, mypy
- `make shell` — dev shell into the control-plane container

**Test (full pyramid, individually runnable)**
- `make test` — everything
- `make test-unit` / `make test-integration` / `make test-e2e` / `make test-smoke` / `make test-regression`

**Eval & demo (the proof surface)**
- `make eval` — run all ≥5 eval tasks in stub mode against oracles
- `make eval-task TASK=<id>` — single task
- `make redteam` — injection + secret-exfil defense tasks specifically
- `make demo-happy` — scripted happy-path run (walkthrough)
- `make demo-resume` — kill mid-task, show crash-resume with no double-charge
- `make demo-budget` — trigger budget exhaustion, show clean stop + partial

**Operate**
- `make metrics` — dump `/metrics`
- `make trace TASK=<id>` — print a run's journal (the journal *is* the per-run trace)

**Env overrides** thread through without new targets: `make eval SANDBOX_RUNNER=go`, `make up MODEL_BACKEND=claude`. This is how the same interface drives the Phase 8 polyglot runners.

---

## 6. Phase-by-Phase Execution

Each phase = one branch, its own tests + oracle, its DESIGN.md section, and any ADR whose decision it makes. **No phase merges until its oracle is green.**

### Phase 0 — Skeleton & Contracts
Repo layout; bounded module interfaces (Protocol/ABC) with stubs: `gateway/`, `retrieval/`, `orchestrator/`, `model_gateway/`, `workspace/`, `sandbox_client/`, `config/`, `db/`, `common/`. `pydantic-settings` config, all env-driven, `.env.example`. SQLite schema in WAL mode: `users`, `api_keys` (hashed only), `workspaces`, `tasks`, `journal` (append-only, unique `(task_id, step_index)`), `budget_ledger`, `artifacts`; typed data-access layer, no raw SQL in business modules. FastAPI `/healthz` `/readyz`. Structured JSON logging + `/metrics` stub. `docker-compose.yml` (control plane + placeholder sandbox host, private network, only gateway publishes a port), **`Makefile` as the thin command interface with auto-generated `make help`** (lifecycle/setup/develop/test targets scaffolded now, each delegating one-line to the `agentctl` CLI or compose; demo targets stubbed), the `agentctl` Python CLI skeleton (Typer/Click), Poetry `Dockerfile`.
**Oracle:** `make up` boots keyless; `/healthz` + `/readyz` → 200; `make test` green; no internal module publishes a port; API keys stored hashed.
**Docs:** DESIGN.md skeleton + data-flow diagram draft; README bring-up section.

### Phase 1 — Workspace & Structural Index
Ingest repo (Git URL or archive) → per-user workspace. `py-tree-sitter` symbol table (functions, classes, methods, types) + references/call sites + import/file graph, queryable across files, for **Python + TypeScript**. Incremental re-index on edit. **Design the synthetic sample repo here** (Python+TS `/users` service with callers + tests; a symbol used across many files; a planted adversarial instruction in a docstring/comment; a fake `.env` secret positioned to test redaction).
**Oracle:** a symbol defined in one file resolves to all N call sites across both languages; editing a file re-indexes only affected symbols.
**Docs:** DESIGN.md retrieval section (structural rationale); ADR-001 (retrieval model) started; ADR-002 (isolation) — workspace scoping portion.

### Phase 2 — Budgeted Retrieval API
Primitives: `search_symbols`, `definition`, `references`, `read_span`, `list_dir`, `structural_grep`. Every call **token-accounted** against the ledger. Deterministic replay per workspace snapshot. **Secret redaction at the retrieval boundary.** Optional Chroma semantic complement.
**Oracle:** reading a whole file costs strictly more budget than a span; same query on same snapshot → identical bytes; a planted secret never appears in retrieval output.
**Docs:** ADR-001 (retrieval model) finalized; DESIGN.md metering section — retrieval-bytes dimension.

### Phase 3 — Sandbox & Real Verification
Sandbox **contract** + **Python reference runner**: apply patch → build/type-check → run repo tests, in Docker with `--network=none`, cgroup CPU/mem/disk, wall-clock kill-on-deadline, dropped caps, non-root, read-only rootfs + tmpfs. All executed code treated as untrusted.
**Oracle:** a network call fails closed; an infinite loop is killed at wall-clock; a passing change exits 0; a broken change surfaces the *real* failure text.
**Docs:** ADR-003 (sandbox technology) — including the sandbox-as-contract framing and gVisor/Firecracker production path; DESIGN.md sandbox & egress section.

### Phase 4 — Agent Loop
Hand-written state machine: **plan → retrieve → edit → verify → repair**, bounded by step + token budgets. Edits proposed as **span patches**, not whole-file blobs. Strict **XML instruction/data trust boundary**; model acts only via the tool allowlist. Append-only **journal** with `(task_id, step)` idempotency → resume without double-charge or re-apply. Per-task isolated worktree; concurrent-write conflict detection (advisory lock + base-commit check). SSE progress stream. Exactly three terminal states: verified-success / give-up-with-reason / budget-exhausted.
**Oracle:** single-file change verifies end-to-end; kill mid-run → resume with no double-charge, no re-applied patch; two concurrent tasks never clobber.
**Docs:** ADR-002 (isolation) finalized; ADR-004 (delivery guarantee) — effectively-once; DESIGN.md agent-loop + isolation sections.

### Phase 5 — Stretch: Self-Repair, then Multi-File Rename
Loop reads real sandbox failure output → re-plans within budget (self-repair first). Then rename a symbol across all references + tests using the call graph.
**Oracle:** seeded failing-first-patch task recovers within budget; rename updates every call site the index knows about + associated tests.
**Docs:** DESIGN.md agent-loop section extended with repair + multi-file reasoning.

### Phase 6 — API / Auth / Metering Wrapper + Budget Enforcement
Hashed API keys, auth on every endpoint, per-key rate limit. `POST /v1/tasks` (apply / dry_run) + SSE `/v1/tasks/{id}/events`. Server-side budget stop with clean checkpoint + partial report. Per-user + per-task metering (tokens in/out, tool calls, retrieval bytes, sandbox seconds).
**Oracle:** no auth → 401; budget exhausted mid-task → clean stop, workspace intact, partial reported; consumer has no path to index/sandbox/model.
**Docs:** DESIGN.md metering & $/task section finalized; auth rationale; SLOs + capacity + runbook.

### Phase 7 — Eval Harness, Full Test Pyramid, Docs Finalization
`eval/` with ≥5 stub-mode tasks + programmatic oracles, incl. ≥1 defended injection + ≥1 defended secret-exfil. Complete test pyramid (unit/integration/e2e/smoke/regression). **Wire up the walkthrough demo targets** (`make demo-happy`, `make demo-resume`, `make demo-budget`, `make redteam`) as scripted, repeatable `agentctl` flows. Finalize DESIGN.md, all 4 ADRs, "setting the bar" note, README (commands section mirrors `make help`). Script the walkthrough as `make demo-*` invocations.
**Oracle:** `make eval` passes all tasks in stub mode; held-out task structure is drop-in; all docs present.

### Phase 8 — Polyglot Sandbox CLIs (strictly last, gated)
Go / Rust / TypeScript runners implementing the **same sandbox contract**; config-swappable (`SANDBOX_RUNNER=python|go|rust|ts`); the **same Phase 3 oracle suite runs against each** to prove parity.
**Oracle:** each runner passes the Phase 3 oracle identically; swapping runners requires only a config change.
**Docs:** ADR-003 extended with parity results; DESIGN.md notes the four conforming implementations as the "own & swap the core" evidence.

### Phase 9 — Observability Dashboard (strictly last, pure polish)
Static vanilla HTML/CSS/JS bundle (dark theme, KPI-card + per-run timeline + filterable run-table layout), served by the gateway as a static asset. Reads **only** via the API + structured logs/journal — no privileged access to index/sandbox/model, so it cannot violate the API-only requirement. Renders data already emitted: KPI cards (tasks by terminal state, injections defended, secrets redacted, sandbox pass/fail, $ spent vs budget); per-run timeline (journal rendered as the plan→retrieve→edit→verify→repair state machine with per-step tokens/bytes/sandbox-seconds); filterable run table; live log tail for a selected run. `make logs` opens snapshot; `make logs LIVE=1` auto-refreshes by polling `/metrics` + events.
**Oracle:** the dashboard renders correctly for real runs (happy, resume, budget-exhausted, injection) using only public endpoints; grepping its code confirms zero privileged/internal calls.
**Docs:** DESIGN.md operability section notes the dashboard as an unprivileged consumer — evidence the API-only boundary holds without a backdoor.

---

## 7. Pre-Submission Self-Check (from the spec)

- [ ] `docker-compose up` brings the entire stack up in stub mode, no external keys.
- [ ] A change is reported done only after a real build + test pass inside the sandbox.
- [ ] No consumer path reaches the index, sandbox, or model directly.
- [ ] Cross-user isolation holds, and we tried to break it.
- [ ] ≥1 planted prompt-injection defended in eval; a secret never leaves the sandbox.
- [ ] A killed run resumes without double-charging or re-applying side effects.
- [ ] Budgets stop a task cleanly with partial progress reported, no overrun.
- [ ] DESIGN.md, README.md, docs/adr/, eval/ all present.
- [ ] Git history incremental; an ADR explains each hard decision.
- [ ] Walkthrough shows happy path, one failure/recovery, one defended injection.

---

## 8. Governance Rules (how we work)

1. One phase at a time. No phase merges until its oracle is green.
2. The retrieval model, agent loop, isolation model, and sandbox are hand-owned — no framework hides them.
3. Each hard decision gets an ADR *in the phase where it is made*, never backfilled.
4. Each phase writes its DESIGN.md section as it lands.
5. Branch per phase; small reviewable commits; no squashed dump.
6. After each phase, review module interfaces / schema before authorizing the next — especially the journal and ledger, which back the resume and no-double-charge guarantees.