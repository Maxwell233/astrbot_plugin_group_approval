"""SQLite database connection and query helpers using aiosqlite."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import aiosqlite

from .migrations import run_migrations


class Database:
    """Asynchronous SQLite database wrapper with WAL mode and transaction support."""

    def __init__(self, db_path: Path | str):
        self.db_path = str(db_path)
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._tx_lock = asyncio.Lock()

    async def init(self) -> None:
        """Initialize connection, pragmas, and run schema migrations."""
        async with self._lock:
            if self._conn is not None:
                return

            # Ensure parent directories exist
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

            self._conn = await aiosqlite.connect(self.db_path)
            self._conn.row_factory = aiosqlite.Row

            # WAL mode and pragmas for concurrency and durability
            await self._conn.execute("PRAGMA journal_mode = WAL;")
            await self._conn.execute("PRAGMA synchronous = NORMAL;")
            await self._conn.execute("PRAGMA foreign_keys = ON;")
            await self._conn.execute("PRAGMA busy_timeout = 5000;")
            await self._conn.commit()

            # Execute schema migrations
            await run_migrations(self._conn)

    async def get_connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            await self.init()
        assert self._conn is not None
        return self._conn

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Execute an atomic write transaction with BEGIN IMMEDIATE and explicit rollback."""
        async with self._tx_lock:
            conn = await self.get_connection()
            await conn.execute("BEGIN IMMEDIATE;")
            try:
                yield conn
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

    async def close(self) -> None:
        """Close connection cleanly."""
        async with self._lock:
            if self._conn is not None:
                await self._conn.close()
                self._conn = None

    async def execute(self, query: str, parameters: tuple[Any, ...] = ()) -> aiosqlite.Cursor:
        conn = await self.get_connection()
        return await conn.execute(query, parameters)

    async def executemany(self, query: str, parameters: list[tuple[Any, ...]]) -> aiosqlite.Cursor:
        conn = await self.get_connection()
        return await conn.executemany(query, parameters)

    async def fetchone(self, query: str, parameters: tuple[Any, ...] = ()) -> aiosqlite.Row | None:
        conn = await self.get_connection()
        async with conn.execute(query, parameters) as cursor:
            return await cursor.fetchone()

    async def fetchall(self, query: str, parameters: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        conn = await self.get_connection()
        async with conn.execute(query, parameters) as cursor:
            return await cursor.fetchall()

    async def commit(self) -> None:
        conn = await self.get_connection()
        await conn.commit()

    async def rollback(self) -> None:
        conn = await self.get_connection()
        await conn.rollback()
