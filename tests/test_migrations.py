"""First-install and repeated initialization guarantees for the scoped schema."""

from sqlite3 import IntegrityError

import pytest

from approval.database import Database
from tests.test_scope_service import BOT, PRIMARY, USER


async def test_new_database_creates_only_current_tables(tmp_path):
    db = Database(tmp_path / "new.db")
    await db.init()
    try:
        tables = {r["name"] for r in await db.fetchall(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )}
        assert tables == {"bot_settings", "scope_policies", "applications", "grants", "primary_group_levels",
                          "grant_restrictions", "reviewer_overrides", "rate_limits", "audit_log", "notification_outbox"}
        grant_columns = {r["name"] for r in await db.fetchall("PRAGMA table_info(grants)")}
        assert {"bot_self_id", "user_id", "scope", "revision"} <= grant_columns
        assert not {"group_id", "expires_at"} & grant_columns
    finally:
        await db.close()


async def test_repeated_initialization_preserves_current_records(scoped):
    service, napcat = scoped
    napcat.set_member(PRIMARY, USER, "25")
    await service.apply(BOT, USER, "GROUP", "")
    await service.apply(BOT, USER, "PRIVATE", "")
    before = await service.overview(BOT, include_audit=True)
    reopened = Database(service.db.db_path)
    try:
        await reopened.init()
        await reopened.init()
        assert (await reopened.fetchone("SELECT COUNT(*) FROM grants"))[0] == len(before["grants"])
        assert (await reopened.fetchone("SELECT COUNT(*) FROM applications"))[0] == len(before["applications"])
        assert (await reopened.fetchone("SELECT COUNT(*) FROM audit_log"))[0] == len(before["audit"])
        assert (await reopened.fetchone("SELECT primary_group_id FROM bot_settings WHERE bot_self_id = ?", (BOT,)))[0] == PRIMARY
    finally:
        await reopened.close()


async def test_database_enforces_scope_unique_keys(temp_db):
    query = """INSERT INTO applications (id, bot_self_id, user_id, scope, primary_group_id, primary_revision,
               level, status, created_at) VALUES (?, ?, ?, ?, ?, 1, 25, 'PENDING', 1)"""
    await temp_db.execute(query, ("L1", BOT, USER, "GROUP", PRIMARY))
    await temp_db.execute(query, ("L2", BOT, USER, "PRIVATE", PRIMARY))
    with pytest.raises(IntegrityError):
        await temp_db.execute(query, ("L3", BOT, USER, "GROUP", PRIMARY))
    grant = """INSERT INTO grants (bot_self_id, user_id, scope, status, source, actor_id, updated_at)
               VALUES (?, ?, 'GROUP', 'ACTIVE', 'OWNER_MANUAL', '99999', 1)"""
    await temp_db.execute(grant, (BOT, USER))
    with pytest.raises(IntegrityError):
        await temp_db.execute(grant, (BOT, USER))
