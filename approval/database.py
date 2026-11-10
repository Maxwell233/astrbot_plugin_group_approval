"""SQLite access layer with explicit, coroutine-isolated transaction contexts.

Design rationale (see docs/SECURITY.md "Database transaction isolation"):

The previous implementation owned a single shared aiosqlite connection and tracked
transaction ownership with a process-global boolean ``_in_transaction``. That is not
coroutine isolation: while coroutine A was inside a transaction, coroutine B kept
issuing statements on the *same* connection, so B's writes joined A's uncommitted
transaction and were committed or rolled back with it. ``Database.commit()`` also
silently returned early for every caller whenever *any* transaction was open.

This module replaces that with two independent mechanisms:

1. **A dedicated connection per transaction.** ``transaction()`` opens a brand new
   SQLite connection, issues ``BEGIN IMMEDIATE`` on it, and owns its commit/rollback
   and close. Two concurrent transactions therefore never share a connection.
2. **A ``contextvars`` handle for explicit transaction scope.** Inside the
   ``transaction()`` block, database helpers automatically route to the transaction's
   connection. ``contextvars`` are per-``task`` in asyncio, so a *different* coroutine
   that is not inside the context (or is running in a separate task) routes to the
   shared autocommit connection instead of joining the open transaction.

The shared connection is opened with ``isolation_level=None`` (true autocommit), so a
statement executed outside an explicit transaction is durably committed on its own and
can never be swept into another coroutine's transaction or rolled back by it.

Notes:
* ``transaction()`` is re-entrant for the *same* task: a nested call joins the
  already-open transaction instead of deadlocking on the write lock.
* ``commit()``/``rollback()`` outside an explicit transaction are no-ops (autocommit
  already persisted the statement). Inside a transaction they are no-ops too, because
  the context manager owns the transaction boundary; a service function can therefore
  never commit or roll back its caller's transaction.
* Transaction bodies must not perform network I/O. Services are expected to enqueue
  outbox rows inside the transaction and let the background worker perform delivery
  after the commit.
* A task spawned *inside* a transaction would inherit the contextvar (asyncio copies
  the current context). It must not touch the database: the transaction connection is
  closed when the block exits, so such use fails closed rather than silently joining
  the transaction.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import aiosqlite

from .schema import initialize_schema

logger = logging.getLogger("astrbot_plugin_group_approval.database")

_CONNECTION_PRAGMAS = (
    "PRAGMA journal_mode = WAL;",
    "PRAGMA synchronous = NORMAL;",
    "PRAGMA foreign_keys = ON;",
    "PRAGMA busy_timeout = 5000;",
)


class Database:
    """Asynchronous SQLite database wrapper with coroutine-isolated transactions."""

    def __init__(self, db_path: Path | str):
        self.db_path = str(db_path)
        self._shared_conn: aiosqlite.Connection | None = None
        self._init_lock = asyncio.Lock()
        self._tx_lock = asyncio.Lock()
        self._transaction_state: contextvars.ContextVar[
            tuple[asyncio.Task[Any] | None, aiosqlite.Connection] | None
        ] = contextvars.ContextVar("group_approval_transaction", default=None)

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------
    async def _open(self, *, pooled: bool) -> aiosqlite.Connection:
        """Open a connection with autocommit semantics and the project pragmas."""
        conn = await aiosqlite.connect(self.db_path, isolation_level=None)
        conn.row_factory = aiosqlite.Row
        for pragma in _CONNECTION_PRAGMAS:
            await conn.execute(pragma)
        if pooled:
            # WAL is a database property; keep the statement for clarity on first open.
            logger.debug("Opened shared autocommit connection to %s", self.db_path)
        return conn

    async def init(self) -> None:
        """Initialize the shared connection, pragmas, and initial schema."""
        async with self._init_lock:
            if self._shared_conn is not None:
                return
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            conn = await self._open(pooled=True)
            try:
                await initialize_schema(conn)
            except BaseException:
                await conn.close()
                raise
            self._shared_conn = conn

    async def get_connection(self) -> aiosqlite.Connection:
        """Return the shared autocommit connection (transaction connections are internal)."""
        if self._shared_conn is None:
            await self.init()
        if self._shared_conn is None:  # pragma: no cover - defensive
            raise RuntimeError("Database is not initialized")
        return self._shared_conn

    def _resolve(self, conn: aiosqlite.Connection | None) -> aiosqlite.Connection | None:
        """Resolve an explicit connection or the active transaction's connection."""
        state = self._transaction_state.get()
        if state is None:
            return conn
        owner, connection = state
        if owner is not asyncio.current_task():
            raise RuntimeError("SQLite transaction connection is restricted to its owner task")
        return conn if conn is not None else connection

    @property
    def in_transaction(self) -> bool:
        """Whether the *current task* is inside an explicit transaction."""
        return self._resolve(None) is not None

    # ------------------------------------------------------------------
    # Transactions
    # ------------------------------------------------------------------
    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Run a block inside an atomic write transaction on a dedicated connection.

        All database helpers called from within the block (same task) automatically use
        the returned connection. Nested calls from the same task join the outer
        transaction. Concurrent tasks are isolated: they use their own connection.
        """
        existing = self._resolve(None)
        if existing is not None:
            # Re-entrant use from the same task: join the transaction already owned by
            # this task. The outermost context manager owns commit/rollback/close.
            yield existing
            return

        # Serialize transaction *beginnings* so that writer contention surfaces as a
        # short wait rather than as SQLITE_BUSY under load. The lock is intentionally
        # held for the duration of the transaction: writes are short and never await
        # external I/O inside the block.
        async with self._tx_lock:
            await self.get_connection()  # guarantees schema initialization
            conn = await self._open(pooled=False)
            token = self._transaction_state.set((asyncio.current_task(), conn))
            try:
                await conn.execute("BEGIN IMMEDIATE;")
                try:
                    yield conn
                except BaseException:
                    await conn.rollback()
                    raise
                try:
                    await conn.commit()
                except BaseException:
                    await conn.rollback()
                    raise
            finally:
                self._transaction_state.reset(token)
                await conn.close()

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------
    async def execute(
        self,
        query: str,
        parameters: tuple[Any, ...] = (),
        conn: aiosqlite.Connection | None = None,
    ) -> aiosqlite.Cursor:
        c = self._resolve(conn) or await self.get_connection()
        return await c.execute(query, parameters)

    async def executemany(
        self,
        query: str,
        parameters: list[tuple[Any, ...]],
        conn: aiosqlite.Connection | None = None,
    ) -> aiosqlite.Cursor:
        c = self._resolve(conn) or await self.get_connection()
        return await c.executemany(query, parameters)

    async def fetchone(
        self,
        query: str,
        parameters: tuple[Any, ...] = (),
        conn: aiosqlite.Connection | None = None,
    ) -> aiosqlite.Row | None:
        c = self._resolve(conn) or await self.get_connection()
        async with c.execute(query, parameters) as cursor:
            return await cursor.fetchone()

    async def fetchall(
        self,
        query: str,
        parameters: tuple[Any, ...] = (),
        conn: aiosqlite.Connection | None = None,
    ) -> list[aiosqlite.Row]:
        c = self._resolve(conn) or await self.get_connection()
        async with c.execute(query, parameters) as cursor:
            return await cursor.fetchall()

    async def commit(self) -> None:
        """Commit helper that can never end a caller's explicit transaction.

        Outside a transaction the shared connection is in autocommit mode, so this is a
        no-op kept for backward compatibility with callers that pair execute()+commit().
        Inside a transaction the surrounding context manager owns the boundary, so this
        is a no-op there as well.
        """
        if self.in_transaction:
            return
        conn = await self.get_connection()
        await conn.commit()

    async def rollback(self) -> None:
        """Rollback helper that can never discard a transaction it does not own."""
        if self.in_transaction:
            return
        conn = await self.get_connection()
        await conn.rollback()

    async def close(self) -> None:
        """Close the shared connection cleanly."""
        async with self._init_lock:
            if self._shared_conn is not None:
                await self._shared_conn.close()
                self._shared_conn = None
