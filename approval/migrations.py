"""Database migrations and schema definitions for Group Approval."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import aiosqlite

MIGRATION_V1_SQL = """
-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at INTEGER NOT NULL
);

-- Per-group admission policy
CREATE TABLE IF NOT EXISTS group_policies (
    bot_self_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    min_level INTEGER NOT NULL DEFAULT 10,
    approval_required INTEGER NOT NULL DEFAULT 1,
    grant_duration_seconds INTEGER NOT NULL DEFAULT 0,
    recheck_interval_seconds INTEGER NOT NULL DEFAULT 300,
    restore_policy TEXT NOT NULL DEFAULT 'automatic',
    owner_bypass INTEGER NOT NULL DEFAULT 1,
    group_admin_bypass INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (bot_self_id, group_id)
);

-- Access applications submitted by users
CREATE TABLE IF NOT EXISTS applications (
    id TEXT PRIMARY KEY,
    bot_self_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    level_at_application INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    reason TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    reviewed_at INTEGER,
    reviewer_id TEXT,
    review_note TEXT
);

CREATE INDEX IF NOT EXISTS idx_apps_user_group ON applications (bot_self_id, group_id, user_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS uq_apps_pending ON applications (bot_self_id, group_id, user_id) WHERE status = 'PENDING';

-- User grants
CREATE TABLE IF NOT EXISTS grants (
    id TEXT PRIMARY KEY,
    bot_self_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    source_application_id TEXT,
    approved_by TEXT NOT NULL DEFAULT '',
    approved_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL DEFAULT 0,
    last_verified_level INTEGER NOT NULL DEFAULT 0,
    last_verified_at INTEGER NOT NULL DEFAULT 0,
    suspend_reason TEXT,
    revoked_by TEXT,
    revoked_at INTEGER,
    UNIQUE (bot_self_id, group_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_grants_lookup ON grants (bot_self_id, group_id, user_id, status);

-- Blacklist
CREATE TABLE IF NOT EXISTS blacklist (
    bot_self_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bot_self_id, group_id, user_id)
);

-- Audit log
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp INTEGER NOT NULL,
    actor_id TEXT NOT NULL,
    actor_type TEXT NOT NULL,
    bot_self_id TEXT NOT NULL,
    group_id TEXT NOT NULL,
    target_user_id TEXT NOT NULL,
    action TEXT NOT NULL,
    old_state TEXT,
    new_state TEXT,
    request_id TEXT,
    details_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log (timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_target ON audit_log (target_user_id, group_id);

-- Outbox for persistent notifications
CREATE TABLE IF NOT EXISTS notification_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL,
    recipient_id TEXT NOT NULL,
    bot_self_id TEXT NOT NULL DEFAULT '',
    fallback_group TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL DEFAULT 'private',
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_retry_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    sent_at INTEGER,
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_outbox_pending ON notification_outbox (status, next_retry_at);
"""


MIGRATION_V2_SQL = """
-- Persistent Reviewers table
CREATE TABLE IF NOT EXISTS reviewers (
    qq_id TEXT PRIMARY KEY,
    added_by TEXT NOT NULL,
    added_at INTEGER NOT NULL
);

-- Application rate limiting table
CREATE TABLE IF NOT EXISTS rate_limits (
    key TEXT PRIMARY KEY,
    count INTEGER NOT NULL,
    window_start INTEGER NOT NULL
);

-- Ensure unique event_key on notification_outbox to prevent duplicate enqueue
CREATE UNIQUE INDEX IF NOT EXISTS uq_outbox_event_key ON notification_outbox (event_key);
"""


async def apply_v1(db: aiosqlite.Connection) -> None:
    await db.executescript(MIGRATION_V1_SQL)
    await db.execute(
        "INSERT OR REPLACE INTO schema_version (version, applied_at) VALUES (1, ?)",
        (int(time.time()),),
    )
    await db.commit()


async def apply_v2(db: aiosqlite.Connection) -> None:
    # 1. Execute new tables and unique constraints
    await db.executescript(MIGRATION_V2_SQL)

    # 2. Check and add columns to grants table if not existing
    async with db.execute("PRAGMA table_info(grants)") as cursor:
        cols = [r[1] for r in await cursor.fetchall()]

    if "grant_type" not in cols:
        await db.execute("ALTER TABLE grants ADD COLUMN grant_type TEXT NOT NULL DEFAULT 'LEVEL_APPROVAL'")
    if "is_level_exempt" not in cols:
        await db.execute("ALTER TABLE grants ADD COLUMN is_level_exempt INTEGER NOT NULL DEFAULT 0")

    await db.execute(
        "INSERT OR REPLACE INTO schema_version (version, applied_at) VALUES (2, ?)",
        (int(time.time()),),
    )
    await db.commit()


async def apply_v3(db: aiosqlite.Connection) -> None:
    # Check and add multi-bot and fallback columns to notification_outbox if missing
    async with db.execute("PRAGMA table_info(notification_outbox)") as cursor:
        cols = [r[1] for r in await cursor.fetchall()]

    if "bot_self_id" not in cols:
        await db.execute("ALTER TABLE notification_outbox ADD COLUMN bot_self_id TEXT NOT NULL DEFAULT ''")
    if "fallback_group" not in cols:
        await db.execute("ALTER TABLE notification_outbox ADD COLUMN fallback_group TEXT NOT NULL DEFAULT ''")

    await db.execute(
        "INSERT OR REPLACE INTO schema_version (version, applied_at) VALUES (3, ?)",
        (int(time.time()),),
    )
    await db.commit()


MIGRATIONS: list[tuple[int, Callable[[aiosqlite.Connection], Any]]] = [
    (1, apply_v1),
    (2, apply_v2),
    (3, apply_v3),
]


async def run_migrations(db: aiosqlite.Connection) -> int:
    """Run pending migrations and return the current schema version."""
    # Ensure schema_version table exists
    await db.execute(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)"
    )
    await db.commit()

    async with db.execute("SELECT MAX(version) FROM schema_version") as cursor:
        row = await cursor.fetchone()
        current_version = row[0] if (row and row[0] is not None) else 0

    for version, migration_func in MIGRATIONS:
        if version > current_version:
            await migration_func(db)
            current_version = version

    return current_version
