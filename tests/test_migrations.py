"""Migration upgrade tests: legacy databases must upgrade without data loss.

The plugin promises backward compatibility with existing V1/V2/V3 databases. These tests
build a genuine legacy V1 schema, insert production-like rows (including duplicate outbox
event keys that used to abort migration V2), then run the real migration chain.
"""

from __future__ import annotations

import time

import aiosqlite
import pytest

from approval.migrations import MIGRATION_V1_SQL, run_migrations


async def _build_legacy_v1(db_path) -> None:
    conn = await aiosqlite.connect(db_path)
    try:
        await conn.executescript(MIGRATION_V1_SQL)
        # Legacy rows that must survive the upgrade.
        now = int(time.time())
        await conn.execute(
            """
            INSERT INTO applications (id, bot_self_id, group_id, user_id, level_at_application,
                status, reason, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            ("A1001", "bot1", "10001", "30001", 20, "APPROVED", "legacy", now, now + 86400),
        )
        await conn.execute(
            """
            INSERT INTO grants (id, bot_self_id, group_id, user_id, status, approved_by,
                approved_at, expires_at, last_verified_level, last_verified_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            ("G1001", "bot1", "10001", "30001", "ACTIVE", "99999", now, 0, 20, now),
        )
        await conn.execute(
            "INSERT INTO group_policies (bot_self_id, group_id, min_level, created_at, updated_at) VALUES (?,?,?,?,?)",
            ("bot1", "10001", 15, now, now),
        )
        # Duplicate event_keys: migration V2 used to create a UNIQUE INDEX directly and abort.
        for payload in ("first", "second", "third"):
            await conn.execute(
                """
                INSERT INTO notification_outbox (event_key, recipient_id, channel, payload,
                    status, attempts, next_retry_at, created_at)
                VALUES ('DUP_EVENT', '30001', 'private', ?, 'PENDING', 0, ?, ?)
                """,
                (payload, now, now),
            )
        await conn.commit()
    finally:
        await conn.close()


async def _schema_version(db_path) -> int:
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    try:
        async with conn.execute("SELECT MAX(version) AS v FROM schema_version") as cur:
            row = await cur.fetchone()
            return int(row["v"] or 0)
    finally:
        await conn.close()


async def _columns(db_path, table: str) -> set[str]:
    conn = await aiosqlite.connect(db_path)
    try:
        async with conn.execute(f"PRAGMA table_info({table})") as cur:
            return {r[1] for r in await cur.fetchall()}
    finally:
        await conn.close()


async def test_legacy_v1_database_upgrades_without_data_loss(tmp_path):
    db_path = str(tmp_path / "legacy.db")
    await _build_legacy_v1(db_path)

    conn = await aiosqlite.connect(db_path)
    try:
        version = await run_migrations(conn)
    finally:
        await conn.close()

    assert version >= 6
    assert await _schema_version(db_path) >= 6

    # Original business rows preserved.
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    try:
        async with conn.execute("SELECT status FROM applications WHERE id='A1001'") as cur:
            assert (await cur.fetchone())["status"] == "APPROVED"
        async with conn.execute("SELECT status, last_verified_level FROM grants WHERE id='G1001'") as cur:
            row = await cur.fetchone()
            assert row["status"] == "ACTIVE"
            assert row["last_verified_level"] == 20
        async with conn.execute("SELECT min_level FROM group_policies WHERE bot_self_id='bot1'") as cur:
            assert (await cur.fetchone())["min_level"] == 15
    finally:
        await conn.close()

    # New columns and tables exist.
    grant_cols = await _columns(db_path, "grants")
    assert "revision" in grant_cols
    assert "grant_type" in grant_cols
    assert "is_level_exempt" in grant_cols
    outbox_cols = await _columns(db_path, "notification_outbox")
    assert "fallback_payload" in outbox_cols
    assert "claimed_at" in outbox_cols

    # Duplicate legacy outbox keys were collapsed instead of aborting the migration.
    conn = await aiosqlite.connect(db_path)
    try:
        async with conn.execute("SELECT COUNT(*) FROM notification_outbox WHERE event_key='DUP_EVENT'") as cur:
            assert (await cur.fetchone())[0] == 1
    finally:
        await conn.close()


async def test_migrations_are_idempotent(tmp_path):
    db_path = str(tmp_path / "idempotent.db")
    conn = await aiosqlite.connect(db_path)
    try:
        first = await run_migrations(conn)
        second = await run_migrations(conn)
    finally:
        await conn.close()
    assert first == second


async def test_migration_preserves_existing_bot_id_and_no_placeholder_rows(tmp_path):
    """Upgrading must not rewrite history to a placeholder bot id."""
    db_path = str(tmp_path / "placeholder.db")
    await _build_legacy_v1(db_path)
    conn = await aiosqlite.connect(db_path)
    try:
        await run_migrations(conn)
        async with conn.execute("SELECT DISTINCT bot_self_id FROM grants") as cur:
            ids = {r[0] for r in await cur.fetchall()}
    finally:
        await conn.close()
    assert ids == {"bot1"}
    assert "default" not in ids
