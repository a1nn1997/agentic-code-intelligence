"""B-3 oracle: the retrieval budget charge is atomic under concurrency.

The old ``RetrievalServiceImpl._charge`` did check-then-act across *separate*
transactions: it read ``remaining_tokens()`` (its own read transaction) and then
wrote RESERVE/COMMIT/RELEASE in three more separate write transactions. Two
concurrent retrievals on the SAME scope could therefore both read the
pre-charge balance, both pass the ceiling check, and both commit — overshooting
the budget the docstring falsely claimed was atomic.

``LedgerRepo.charge_atomic`` does the SELECT-SUM balance read AND the three
inserts under ONE ``BEGIN IMMEDIATE``, so a second charger blocks until the
first commits and then reads the post-charge balance. These tests hammer one
scope from N threads with a budget that admits only a few charges and assert:

  * committed spend NEVER exceeds the ceiling (the load-bearing invariant), and
  * exactly ``budget // cost`` charges succeed; the rest are refused with
    :class:`BudgetExceeded` and write nothing.

The ceiling-overshoot assertion FAILS on the old per-transaction charge (two+
concurrent charges slip past the check) and PASSES on ``charge_atomic``.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from acp.common.errors import BudgetExceeded
from acp.db import Database, LedgerRepo, init_db

pytestmark = pytest.mark.integration

_N_THREADS = 16
_COST_PER_CHARGE = 100
_BUDGET = 500  # admits exactly 5 charges; the other 11 must be refused
_SCOPE = "task:budget-toctou-probe"


@pytest.fixture
def shared_db(tmp_path: Path) -> Database:
    """One migrated ``Database`` shared across all threads (as the gateway
    shares it across the Starlette threadpool)."""
    db_path = str(tmp_path / "toctou.db")
    init_db(db_path)
    return Database(db_path)


def test_concurrent_charges_never_overshoot_budget(shared_db: Database) -> None:
    """N threads charge the SAME scope at once against a budget of 5 charges.

    Committed spend must never exceed the ceiling and exactly ``budget // cost``
    charges may succeed. On the old cross-transaction charge, multiple threads
    read the same stale balance and overshoot; ``charge_atomic`` serializes the
    check-and-write so the ceiling holds exactly.
    """
    ledger = LedgerRepo(shared_db)
    barrier = threading.Barrier(_N_THREADS)
    granted = 0
    refused = 0
    lock = threading.Lock()

    def charger(i: int) -> None:
        nonlocal granted, refused
        barrier.wait()  # maximize contention: all threads charge at once
        try:
            ledger.charge_atomic(_SCOPE, _COST_PER_CHARGE, _BUDGET, note=f"c{i}")
        except BudgetExceeded:
            with lock:
                refused += 1
        else:
            with lock:
                granted += 1

    threads = [threading.Thread(target=charger, args=(i,)) for i in range(_N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    spent = ledger.spent_tokens(_SCOPE)
    max_charges = _BUDGET // _COST_PER_CHARGE
    assert spent <= _BUDGET, f"budget OVERSHOT: committed {spent} tokens > ceiling {_BUDGET}"
    assert spent == max_charges * _COST_PER_CHARGE, (
        f"expected exactly {max_charges} charges committed, got {spent // _COST_PER_CHARGE}"
    )
    assert granted == max_charges, f"expected {max_charges} grants, got {granted}"
    assert refused == _N_THREADS - max_charges
    # No-write-on-refusal: net reserved returns to 0 (every grant released).
    assert ledger.reserved_tokens(_SCOPE) == 0


def test_refusal_writes_nothing(shared_db: Database) -> None:
    """A single over-budget charge raises and leaves the ledger untouched."""
    ledger = LedgerRepo(shared_db)
    with pytest.raises(BudgetExceeded):
        ledger.charge_atomic(_SCOPE, 100, 50, note="too-big")
    assert ledger.spent_tokens(_SCOPE) == 0
    assert ledger.reserved_tokens(_SCOPE) == 0
