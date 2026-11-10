"""Regression tests for coroutine-isolated SQLite transactions.

These tests exercise the real ``approval.database.Database`` against a real SQLite
file. Nothing is mocked: two genuine asyncio tasks interleave at controlled ``await``
points, which is exactly the failure mode the old single-connection design had.

On the previous implementation these tests fail because a second coroutine's writes
joined the first coroutine's open transaction (same connection + global
``_in_transaction`` flag), so they were rolled back with it or committed early.
"""

from __future__ import annotations

import asyncio
from sqlite3 import OperationalError

import aiosqlite
import pytest

import approval.database as database_module
from approval.database import Database

SCHEMA = "CREATE TABLE IF NOT EXISTS t (id TEXT PRIMARY KEY, v TEXT)"


async def test_failed_connection_setup_closes_connection(tmp_path, monkeypatch):
    connections = []
    real_connect = aiosqlite.connect

    def connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(aiosqlite, "connect", connect)
    monkeypatch.setattr(database_module, "_CONNECTION_PRAGMAS", ("SELECT * FROM missing_table",))
    database = Database(tmp_path / "failed.db")
    try:
        with pytest.raises(OperationalError, match="missing_table"):
            await database.init()
        with pytest.raises(ValueError, match="no active connection"):
            await connections[0].execute("SELECT 1")
    finally:
        for connection in connections:
            await connection.close()


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "isolation.db")
    await database.init()
    await database.execute(SCHEMA)
    try:
        yield database
    finally:
        await database.close()


async def test_uncommitted_transaction_is_invisible_to_another_task(db):
    """A row written inside A's open transaction must not be visible to task B."""
    a_wrote = asyncio.Event()
    b_checked = asyncio.Event()

    async def tx_task():
        async with db.transaction():
            await db.execute("INSERT INTO t (id, v) VALUES (?, ?)", ("A", "uncommitted"))
            assert await db.fetchone("SELECT id FROM t WHERE id = ?", ("A",)) is not None
            a_wrote.set()
            await asyncio.wait_for(b_checked.wait(), timeout=5)
            raise RuntimeError("force rollback")

    async def reader_task():
        await asyncio.wait_for(a_wrote.wait(), timeout=5)
        try:
            return await db.fetchone("SELECT id FROM t WHERE id = ?", ("A",))
        finally:
            b_checked.set()

    tx_result, seen = await asyncio.gather(tx_task(), reader_task(), return_exceptions=True)
    assert isinstance(tx_result, RuntimeError)
    assert seen is None, "another task observed uncommitted data from an open transaction"
    assert await db.fetchone("SELECT id FROM t WHERE id = ?", ("A",)) is None


async def test_closed_database_requires_explicit_initialization(db):
    await db.execute("INSERT INTO t VALUES ('persisted', 'value')")
    await db.close()
    with pytest.raises(RuntimeError, match="Database is not initialized"):
        await db.execute("INSERT INTO t VALUES ('late', 'write')")
    await db.init()
    assert [row["id"] for row in await db.fetchall("SELECT id FROM t")] == ["persisted"]


async def test_rollback_does_not_discard_another_tasks_independent_write(db):
    """Task B's committed write must survive task A's rollback.

    This is the core defect reproduction: on the old design B used the same connection
    as A, so B's insert belonged to A's transaction and was rolled back with it.
    """
    a_wrote = asyncio.Event()
    b_starting = asyncio.Event()

    async def tx_task():
        async with db.transaction():
            await db.execute("INSERT INTO t (id, v) VALUES (?, ?)", ("A", "uncommitted"))
            a_wrote.set()
            await asyncio.wait_for(b_starting.wait(), timeout=5)
            raise RuntimeError("force rollback")

    async def writer_task():
        await asyncio.wait_for(a_wrote.wait(), timeout=5)
        b_starting.set()
        await db.execute("INSERT INTO t (id, v) VALUES (?, ?)", ("B", "independent"))

    tx_result, write_result = await asyncio.gather(tx_task(), writer_task(), return_exceptions=True)
    assert isinstance(tx_result, RuntimeError), f"tx task should have rolled back, got {tx_result!r}"
    assert write_result is None, f"independent writer failed: {write_result!r}"

    rows = await db.fetchall("SELECT id FROM t ORDER BY id")
    assert [r["id"] for r in rows] == ["B"], "B's independent write must be committed; A's must be rolled back"


async def test_nested_transaction_joins_outer_transaction(db):
    """Nested transaction() from the same task must join, not deadlock or split."""

    async with db.transaction():
        await db.execute("INSERT INTO t (id, v) VALUES (?, ?)", ("outer", "1"))
        async with db.transaction():
            await db.execute("INSERT INTO t (id, v) VALUES (?, ?)", ("inner", "2"))

    rows = await db.fetchall("SELECT id FROM t ORDER BY id")
    assert [r["id"] for r in rows] == ["inner", "outer"]


async def test_concurrent_transactions_are_serialized_and_atomic(db):
    """Two concurrent transactions must both commit without corruption or deadlock."""
    start = asyncio.Event()

    async def worker(name: str):
        await asyncio.wait_for(start.wait(), timeout=5)
        async with db.transaction():
            await db.execute("INSERT INTO t (id, v) VALUES (?, ?)", (name, "1"))
            await asyncio.sleep(0)  # force an interleaving point mid-transaction
            await db.execute("INSERT INTO t (id, v) VALUES (?, ?)", (name + "-2", "2"))

    tasks = [asyncio.create_task(worker("w1")), asyncio.create_task(worker("w2"))]
    start.set()
    results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=15)
    assert results == [None, None], f"concurrent transactions failed: {results!r}"

    rows = await db.fetchall("SELECT id FROM t ORDER BY id")
    assert [r["id"] for r in rows] == ["w1", "w1-2", "w2", "w2-2"]


async def test_failed_statement_rolls_back_whole_transaction(db):
    """Any failure inside the transaction block must roll back every statement."""

    with pytest.raises(Exception):
        async with db.transaction():
            await db.execute("INSERT INTO t (id, v) VALUES (?, ?)", ("a", "1"))
            await db.execute("INSERT INTO t (id, v) VALUES (?, ?)", ("a", "duplicate-pk"))

    rows = await db.fetchall("SELECT id FROM t")
    assert rows == []


async def test_child_task_cannot_join_parent_transaction(db):
    async with db.transaction():
        with pytest.raises(RuntimeError, match="owner"):
            await asyncio.create_task(db.execute("INSERT INTO t VALUES ('child', 'unexpected')"))
    assert await db.fetchone("SELECT id FROM t") is None


async def test_database_instance_cannot_inherit_another_databases_transaction(db, tmp_path):
    other = Database(tmp_path / "other.db")
    await other.init()
    try:
        await other.execute(SCHEMA)
        async with db.transaction():
            await other.execute("INSERT INTO t VALUES ('other', 'separate')")
            assert await db.fetchone("SELECT id FROM t") is None
        assert (await other.fetchone("SELECT id FROM t"))[0] == "other"
    finally:
        await other.close()
