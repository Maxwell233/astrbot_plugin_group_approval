"""Complete 0.1.2 schema for new installations."""

from __future__ import annotations

import aiosqlite

SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS bot_settings (
    bot_self_id TEXT PRIMARY KEY,
    primary_group_id TEXT NOT NULL DEFAULT '',
    primary_revision INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 0,
    cache_ttl INTEGER NOT NULL DEFAULT 300 CHECK (cache_ttl BETWEEN 0 AND 3600),
    group_to_private INTEGER NOT NULL DEFAULT 0 CHECK (group_to_private IN (0, 1)),
    private_to_group INTEGER NOT NULL DEFAULT 0 CHECK (private_to_group IN (0, 1)),
    owner_bypass INTEGER NOT NULL DEFAULT 0 CHECK (owner_bypass IN (0, 1))
);
CREATE TABLE IF NOT EXISTS scope_policies (
    bot_self_id TEXT NOT NULL REFERENCES bot_settings(bot_self_id),
    scope TEXT NOT NULL CHECK (scope IN ('GROUP', 'PRIVATE')),
    min_level INTEGER NOT NULL CHECK (min_level BETWEEN 0 AND 1000),
    approval_required INTEGER NOT NULL CHECK (approval_required IN (0, 1)),
    PRIMARY KEY (bot_self_id, scope)
);
CREATE TABLE IF NOT EXISTS applications (
    id TEXT PRIMARY KEY,
    bot_self_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    scope TEXT NOT NULL CHECK (scope IN ('GROUP', 'PRIVATE')),
    source_group_id TEXT NOT NULL DEFAULT '',
    primary_group_id TEXT NOT NULL,
    primary_revision INTEGER NOT NULL,
    level INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED', 'CANCELLED')),
    created_at INTEGER NOT NULL,
    reviewed_at INTEGER,
    reviewer_id TEXT,
    review_note TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_scope_pending ON applications (bot_self_id, user_id, scope)
    WHERE status = 'PENDING';
CREATE INDEX IF NOT EXISTS idx_scope_applications ON applications (bot_self_id, created_at DESC);
CREATE TABLE IF NOT EXISTS grants (
    bot_self_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    scope TEXT NOT NULL CHECK (scope IN ('GROUP', 'PRIVATE')),
    status TEXT NOT NULL CHECK (status IN ('ACTIVE', 'SUSPENDED', 'REVOKED')),
    source TEXT NOT NULL CHECK (source IN ('MANUAL_APPROVAL', 'AUTO_NO_APPROVAL', 'CROSS_GRANT', 'OWNER_MANUAL')),
    source_application_id TEXT REFERENCES applications(id),
    level_exempt INTEGER NOT NULL DEFAULT 0 CHECK (level_exempt IN (0, 1)),
    membership_exempt INTEGER NOT NULL DEFAULT 0 CHECK (membership_exempt IN (0, 1)),
    actor_id TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bot_self_id, user_id, scope)
);
CREATE TABLE IF NOT EXISTS primary_group_levels (
    bot_self_id TEXT NOT NULL,
    primary_group_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    primary_revision INTEGER NOT NULL,
    level INTEGER NOT NULL,
    verified_at INTEGER NOT NULL,
    lookup_epoch INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bot_self_id, primary_group_id, user_id)
);
CREATE TABLE IF NOT EXISTS grant_restrictions (
    bot_self_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    scope TEXT NOT NULL CHECK (scope IN ('GROUP', 'PRIVATE')),
    kind TEXT NOT NULL CHECK (kind IN ('REVOKED', 'BLACKLIST')),
    PRIMARY KEY (bot_self_id, user_id, scope, kind)
);
CREATE TABLE IF NOT EXISTS reviewer_overrides (
    qq_id TEXT PRIMARY KEY,
    action TEXT NOT NULL CHECK (action IN ('ADDED', 'REMOVED')),
    changed_by TEXT NOT NULL,
    changed_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS rate_limits (
    key TEXT PRIMARY KEY,
    count INTEGER NOT NULL,
    window_start INTEGER NOT NULL
);
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
CREATE TABLE IF NOT EXISTS notification_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE,
    recipient_id TEXT NOT NULL,
    bot_self_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_retry_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    claimed_at INTEGER NOT NULL DEFAULT 0,
    sent_at INTEGER,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending ON notification_outbox (status, next_retry_at);
COMMIT;
"""


async def initialize_schema(db: aiosqlite.Connection) -> None:
    try:
        await db.executescript(SCHEMA)
    except BaseException:
        await db.rollback()
        raise
