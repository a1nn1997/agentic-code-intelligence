# ADR-0006 — Eval-before-merge: programmatic oracles as the merge gate

- **Status:** Accepted (Phase 7).
- **Phase:** 7 (eval harness + this standard adopted together).
- **Deciders:** orchestrator role.

## Context

The platform's defining claim is that a code change is done only when the
sandbox verifies it — the model cannot mark itself done.  We need an analogous
standard for our own development work: a merge gate that asserts the
platform's claimed properties by examining real artifacts, not by human
judgment or coverage %.

## Decision

**Nothing merges until `make eval` returns exit 0 in stub mode (keyless).**

`make eval` runs the declarative eval harness (`eval/runner.py`) against all
registered tasks.  Each task has a *programmatic oracle* — a function that
reads real platform output (TaskStatus, journal entries, artifacts) and returns
`OracleResult(passed: bool, reason: str)`.  No human judgment, no proxy metric.

The eval harness is the living specification of "done."  Adding a capability
means adding an eval task.  Adding an adversarial scenario means adding a
red-team task.  The merge gate enforces it.

## Alternatives considered

**Tests-green-only gate.** Unit and integration tests are the floor.  They do
not prove end-to-end properties like "the injection had zero effect" or "the
secret never appeared in any model prompt."  The eval harness does.

**Manual review.** A reviewer cannot replay a crash-resume scenario, simulate
injection in a 3000-line codebase, or scan every journal entry for a secret
value.  The oracle can.

**Coverage ≥ N%.** Coverage % is a proxy.  80% coverage does not prevent a
double-charge on resume or a secret appearing in an artifact.  Oracles assert
the property directly.

## Consequences

**Positive:**
- The merge gate is machine-checkable and deterministic.
- Oracles are the executable specification; documentation is derived from them.
- The eval harness doubles as a regression suite: adding a test for a fixed bug
  (see `test_regression.py`) means the bug cannot silently return.
- Held-out tasks drop in without code changes (declarative task spec).

**Negative:**
- Writing a good oracle takes effort.  An oracle that just returns `True` defeats
  the purpose; reviewers must check oracle quality, not just green CI.
- Eval runtime grows as tasks are added.  Mitigated by stub mode (< 30s today)
  and Docker-gating for sandbox-heavy tasks.

## Proof

`make eval` is wired in the Makefile as a thin delegate to `agentctl eval`.
The eval harness runs all 5 tasks in stub mode and prints a pass/fail table.
`make redteam` runs the injection + secret-exfil tasks specifically.  Both exit
non-zero if any oracle fails — the merge gate is enforced by the CI runner
checking the exit code.

See also: `docs/setting_the_bar.md` (½-page engineering standard note).
