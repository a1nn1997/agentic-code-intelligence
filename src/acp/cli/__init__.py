"""``agentctl`` — the Python CLI that the Makefile delegates to.

The Makefile is a thin menu; all real behavior lives here (or in compose), so
every operator action is unit-testable code rather than shell logic. Phase 0
implements the lifecycle/setup commands that already have meaning (serve,
migrate, seed, config, trace) and stubs the demo/eval commands that depend on
later phases, each printing what will implement it.
"""

from acp.cli.main import app, main

__all__ = ["app", "main"]
