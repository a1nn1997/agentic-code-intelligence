"""B-CRIT-1 oracle: the ``Database`` facade is safe under concurrent writers.

Starlette runs sync (``def``) endpoints on a threadpool, so the ``Database``
facade is shared across threads. A single shared ``sqlite3.Connection`` breaks
in two ways when two threads write concurrently:

  1. Both enter ``BEGIN IMMEDIATE`` on the *same* connection → SQLite raises
     "cannot start a transaction within a transaction".
  2. Even if that is dodged, one thread's ``COMMIT`` flushes another thread's
     half-written work → a lost/torn write.

``BEGIN IMMEDIATE`` only serializes writers across *separate* connections, so
the fix is a per-thread connection. These tests hammer ONE ``Database`` instance
from N threads through the real repository write path and assert:

  * no "transaction within a transaction" (or any) error is raised, and
  * the committed token total equals the exact sum of what every thread wrote
    (no lost write).

Both assertions FAIL on a single-shared-connection ``Database`` and PASS on the
thread-local implementation.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from acp.common.types import LedgerEntryKind
from acp.db import Database, LedgerRepo, UsersRepo, init_db

pytestmark = pytest.mark.integration

_N_THREADS = 16
_TOKENS_PER_WRITE = 100
_SCOPE = "user:concurrency-probe"


@pytest.fixture
def shared_db(tmp_path: Path) -> Database:
    """One migrated ``Database`` instance, shared across all threads — exactly
    how the gateway shares it across the Starlette threadpool."""
    db_path = str(tmp_path / "concurrency.db")
    init_db(db_path)
    db = Database(db_path)
    UsersRepo(db).create("u")
    return db


def test_concurrent_writes_no_transaction_within_transaction(shared_db: Database) -> None:
    """N threads each do a COMMIT append through the SAME ``Database``.

    On a shared connection this raises "cannot start a transaction within a
    transaction"; with per-thread connections every write succeeds.
    """
    ledger = LedgerRepo(shared_db)
    errors: list[Exception] = []
    barrier = threading.Barrier(_N_THREADS)

    def writer(i: int) -> None:
        try:
            barrier.wait()  # maximize contention: all threads write at once
            ledger.append(
                _SCOPE,
                LedgerEntryKind.COMMIT,
                tokens=_TOKENS_PER_WRITE,
                note=f"writer-{i}",
            )
        except Exception as e:  # noqa: BLE001 — the test records every failure
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(_N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, (
        f"{len(errors)} concurrent writes failed (shared-connection bug). "
        f"First error: {errors[0]!r}"
    )


def test_concurrent_writes_no_lost_write(shared_db: Database) -> None:
    """The committed token total must equal N * tokens-per-write exactly.

    A prematurely-flushed shared transaction loses or double-counts writes; the
    sum then disagrees with the ground truth. Per-thread connections give the
    exact total.
    """
    ledger = LedgerRepo(shared_db)
    barrier = threading.Barrier(_N_THREADS)

    def writer(i: int) -> None:
        barrier.wait()
        ledger.append(_SCOPE, LedgerEntryKind.COMMIT, tokens=_TOKENS_PER_WRITE)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(_N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    expected = _N_THREADS * _TOKENS_PER_WRITE
    actual = ledger.spent_tokens(_SCOPE)
    assert actual == expected, (
        f"lost/torn write: committed {actual} tokens, expected exactly {expected}"
    )
