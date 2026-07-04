"""SQLite connection management and the ``Database`` facade.

WAL mode lets readers and one writer proceed concurrently; ``BEGIN IMMEDIATE``
takes the write lock up front so two concurrent writers serialize cleanly
instead of one failing late with ``SQLITE_BUSY`` after doing work. This pairing
is the concurrency contract the journal and ledger rely on.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

from acp.common.logging import get_logger

_log = get_logger(__name__)

# Wait up to 5s for a competing writer before raising SQLITE_BUSY.
_BUSY_TIMEOUT_MS = 5000


def connect(sqlite_path: str) -> sqlite3.Connection:
    """Open a WAL-mode connection with sane pragmas and row-dict access."""
    path = Path(sqlite_path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        sqlite_path,
        # We manage transactions explicitly (BEGIN IMMEDIATE); disable the
        # implicit one so autocommit + our own BEGIN don't fight.
        isolation_level=None,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS};")
    return conn


def _schema_sql() -> str:
    """Read the bundled schema.sql (packaged as data, not a hard-coded string)."""
    return resources.files("acp.db").joinpath("schema.sql").read_text(encoding="utf-8")


def init_db(sqlite_path: str) -> None:
    """Apply the schema (idempotent — every DDL is ``IF NOT EXISTS``)."""
    conn = connect(sqlite_path)
    try:
        conn.executescript(_schema_sql())
        _log.info("db.migrated", extra={"sqlite_path": sqlite_path})
    finally:
        conn.close()


class Database:
    """Hands out a *per-thread* connection and a write transaction context.

    Writers use :meth:`immediate` (BEGIN IMMEDIATE ... COMMIT/ROLLBACK); reads
    go through the raw connection. Repositories take a ``Database`` and never
    open their own connections, so the transaction boundary has a single owner.

    **Threading model (B-CRIT-1):** Starlette runs sync (``def``) endpoints on a
    threadpool, so a single shared ``sqlite3.Connection`` would let two threads
    collide inside ``BEGIN IMMEDIATE`` ("cannot start a transaction within a
    transaction") or have one thread's COMMIT flush another's half-written work.
    ``BEGIN IMMEDIATE`` only serializes across *separate* connections. We
    therefore keep one connection per thread (a thread-local factory): every
    thread opens its own WAL connection once, with identical pragmas, so the
    WAL + IMMEDIATE serialization the ledger/journal correctness story rests on
    actually holds. The public surface (``conn``/``immediate``/``close``) is
    unchanged.
    """

    def __init__(self, sqlite_path: str) -> None:
        self._sqlite_path = sqlite_path
        self._local = threading.local()
        # Track every connection we open so ``close()`` can release them all.
        self._all_conns: list[sqlite3.Connection] = []
        self._all_conns_lock = threading.Lock()
        self._closed = False

    def _thread_conn(self) -> sqlite3.Connection:
        if self._closed:
            # A closed Database is unreachable from every thread, not just the
            # one that called close(); reopening a thread-local connection here
            # would silently mask an intentional shutdown (and the /readyz
            # DB-unreachable path).
            raise sqlite3.ProgrammingError("Database is closed")
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = connect(self._sqlite_path)
            self._local.conn = conn
            with self._all_conns_lock:
                self._all_conns.append(conn)
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        return self._thread_conn()

    @contextmanager
    def immediate(self) -> Iterator[sqlite3.Connection]:
        """A write transaction that grabs the lock up front (BEGIN IMMEDIATE).

        Commits on clean exit, rolls back on any exception — so a failed write
        never leaves a half-applied journal/ledger entry. Uses the calling
        thread's own connection, so concurrent writers serialize via SQLite's
        write lock rather than corrupting a shared transaction.
        """
        conn = self._thread_conn()
        conn.execute("BEGIN IMMEDIATE;")
        try:
            yield conn
            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise

    def close(self) -> None:
        self._closed = True
        with self._all_conns_lock:
            conns = list(self._all_conns)
            self._all_conns.clear()
        for conn in conns:
            conn.close()
        self._local = threading.local()
