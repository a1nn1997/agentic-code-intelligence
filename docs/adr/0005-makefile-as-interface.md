# ADR-0005 — Makefile as the thin operator interface

- **Status:** Accepted (Phase 0, formalized in Phase 7).
- **Phase:** 0 (scaffolded) → 7 (completed; all demo/eval targets wired).
- **Deciders:** orchestrator role.

## Context

The platform has multiple operator entry-points: starting the stack, running
tests, running the eval harness, demo walkthroughs, sandbox operations, metrics.
Without a unified interface, an evaluator must discover commands from multiple
sources (README, scripts, compose files, CLI help) and there is no single
regression check that exercises all of them.

## Decision

The `Makefile` is the **single operator command interface** — the universal verb
for every lifecycle, test, eval, and demo action.  Every target is a one-line
delegate to:
- `agentctl` (the Python CLI, built with Typer) for business-logic operations, or
- `docker compose` for service lifecycle, or
- `scripts/` for shell-only operations (sandbox build/clean).

No business logic lives in the Makefile itself.  `make help` (the default target)
auto-generates a command menu from `##` comments.  All targets are `.PHONY`.

## Alternatives considered

**README-only commands.** README commands drift from reality; they cannot be
tested.  A Makefile target IS the tested interface — `make test` runs the exact
same command CI runs.

**Separate shell scripts per operation.** Scripts under `scripts/` are
appropriate for shell-only work (Docker build/clean).  But an operator who must
remember "run script X for eval, script Y for demo, docker compose for
lifecycle" has three interfaces, not one.

**Python entry-point CLI only.** `agentctl` is the right place for business
logic; but `agentctl demo-happy` is not as discoverable as `make demo-happy`.
The Makefile is the front door; `agentctl` is the kitchen.

## Consequences

**Positive:**
- `make help` gives an evaluator the complete command surface in one glance.
- Demo targets (`make demo-*`) double as smoke checks (non-zero exit on failure).
- Env overrides (`make eval SANDBOX_RUNNER=go`) thread without new targets.
- CI and human operators use the same verbs.

**Negative:**
- Make syntax is unfamiliar to some contributors; mitigated by keeping targets
  trivially simple (one-liners only).
- Tab vs space indentation in Makefiles is a silent foot-gun; enforced by
  `make lint` failing on malformed targets.

## Proof

`make help` lists all targets.  `make eval` runs the eval harness and exits
non-zero on oracle failure.  `make demo-happy|demo-resume|demo-budget` each
print structured output and exit non-zero on claim failure.  These targets are
the walkthrough script (§4.8 of the execution plan).
