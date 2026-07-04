# Setting the Bar: Eval-Before-Merge

> **One standard we would insist on from day one.**

## The standard

**Nothing is "done" until an automated oracle says it is.**

Specifically: no change merges unless an automated evaluation harness — with
*programmatic* pass/fail criteria — has run against it and returned green.
This is not "tests pass"; it is "the oracle we wrote for this exact capability
has verified the outcome by examining a concrete artifact, not a self-report."

## Why this one

The platform's own thesis is that a code change is done only when the sandbox
verifies it.  The model cannot mark itself done; only a real build-and-test
execution counts.  We insist on exactly the same standard for our own
engineering work.

This project's phase-gate discipline embodies it: *no phase merges until its
programmatic oracle is green.*  The eval harness (`eval/runner.py`) is the
product of that discipline, not an afterthought.

## What it looks like in practice

Every merge-blocking CI job runs `make eval`.  Each eval task is a declarative
spec: an instruction, a budget, and an oracle that asserts a **concrete fact**
about the outcome — "state == verified_success", "every call site in the index
was patched", "the planted secret value is absent from every outbound surface".

Human judgment is not an oracle.  "It seemed fine" is not an oracle.  "The logs
looked reasonable" is not an oracle.  A function that reads structured output and
returns `True` or `False` is an oracle.

## Why not something else

**Code review** is necessary but not sufficient.  A reviewer cannot simulate a
multi-file rename across 30 call sites in their head.  The oracle can.

**Test coverage %** is a proxy metric.  Hitting 90% coverage does not prove that
the rename updated the right files, that the injection was actually ignored, or
that the ledger was not double-charged.  Oracles assert the property directly.

**"All tests green"** is the floor, not the ceiling.  The existing 155-test
suite is the floor.  The eval harness is the ceiling: it proves the properties
the spec actually cares about, end-to-end, in the same mode the evaluator runs.

## The cost

One eval run takes under 30 seconds in stub mode (no Docker, no API key).  The
Docker-gated tiers add minutes but run only when the sandbox contract is
changed.  The cost of a missed regression — a double-charge, a leaked secret, a
corrupted rename — is much higher.

## How it scales

New capabilities add new eval tasks.  New adversarial scenarios add new
red-team tasks.  Held-out repos drop in without code changes (the task spec is
declarative).  The oracle is the living specification of "done."

---

*This note was written in Phase 7 and is the recommended engineering standard
(ADR-0006) for the Agentic Code-Intelligence Platform.*
