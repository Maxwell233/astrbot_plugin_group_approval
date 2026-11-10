"""SQLite autocommit queries and task-owned write transactions.

Each write transaction owns its connection and joins nested scopes in the same
task. Network I/O belongs outside the transaction so it never holds a write lock.
"""

from __future__ import annotations

import asyncio
import contextvars
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import aiosqlite

from .schema import initialize_schema

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
    async def _open(self) -> aiosqlite.Connection:
        """Open a connection with autocommit semantics and the project pragmas."""
        conn = await aiosqlite.connect(self.db_path, isolation_level=None)
        conn.row_factory = aiosqlite.Row
        try:
            for pragma in _CONNECTION_PRAGMAS:
                async with conn.execute(pragma):
                    pass
        except BaseException:
            await conn.close()
            raise
        return conn

    async def init(self) -> None:
        """Initialize the shared connection, pragmas, and initial schema."""
        async with self._init_lock:
            if self._shared_conn is not None:
                return
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            conn = await self._open()
            try:
                await initialize_schema(conn)
            except BaseException:
                await conn.close()
                raise
            self._shared_conn = conn

    def _shared_connection(self) -> aiosqlite.Connection:
        """Return the shared autocommit connection (transaction connections are internal)."""
        if self._shared_conn is None:
            raise RuntimeError("Database is not initialized")
        return self._shared_conn

    def _current_transaction(self) -> aiosqlite.Connection | None:
        state = self._transaction_state.get()
        if state is None:
            return None
        owner, connection = state
        if owner is not asyncio.current_task():
            raise RuntimeError("SQLite transaction connection is restricted to its owner task")
        return connection

    # ------------------------------------------------------------------
    # Transactions
    # ------------------------------------------------------------------
    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        """Run a block inside an atomic write transaction on a dedicated connection.

        All database helpers called from within the block (same task) automatically use
        the transaction's connection. Nested calls from the same task join the outer
        transaction. Concurrent tasks are isolated: they use their own connection.
        """
        if self._current_transaction() is not None:
            # Re-entrant use from the same task: join the transaction already owned by
            # this task. The outermost context manager owns commit/rollback/close.
            yield
            return

        # Serialize transaction *beginnings* so that writer contention surfaces as a
        # short wait rather than as SQLITE_BUSY under load. The lock is intentionally
        # held for the duration of the transaction: writes are short and never await
        # external I/O inside the block.
        async with self._tx_lock:
            self._shared_connection()
            conn = await self._open()
            token = self._transaction_state.set((asyncio.current_task(), conn))
            try:
                await conn.execute("BEGIN IMMEDIATE;")
                try:
                    yield
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
    ) -> aiosqlite.Cursor:
        c = self._current_transaction() or self._shared_connection()
        return await c.execute(query, parameters)

    async def fetchone(
        self,
        query: str,
        parameters: tuple[Any, ...] = (),
    ) -> aiosqlite.Row | None:
        c = self._current_transaction() or self._shared_connection()
        async with c.execute(query, parameters) as cursor:
            return await cursor.fetchone()

    async def fetchall(
        self,
        query: str,
        parameters: tuple[Any, ...] = (),
    ) -> list[aiosqlite.Row]:
        c = self._current_transaction() or self._shared_connection()
        async with c.execute(query, parameters) as cursor:
            return await cursor.fetchall()

    async def close(self) -> None:
        """Close the shared connection cleanly."""
        async with self._init_lock:
            if self._shared_conn is not None:
                await self._shared_conn.close()
                self._shared_conn = None
