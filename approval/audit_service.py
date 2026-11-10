"""Audit logging service for tracking administrative actions and status changes."""

from __future__ import annotations

import json
import time
from typing import Any

from .database import Database


class AuditService:
    """Records security-critical events and provides Dashboard queries."""

    def __init__(self, db: Database):
        self.db = db

    async def log(
        self,
        actor_id: str,
        actor_type: str,
        bot_self_id: str,
        group_id: str,
        target_user_id: str,
        action: str,
        old_state: str | None = None,
        new_state: str | None = None,
        request_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Insert an audit log entry."""
        details_json = json.dumps(details or {}, ensure_ascii=False)
        ts = int(time.time())

        sql = """
        INSERT INTO audit_log (
            timestamp, actor_id, actor_type, bot_self_id, group_id,
            target_user_id, action, old_state, new_state, request_id, details_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            ts,
            str(actor_id).strip(),
            str(actor_type).strip().upper(),
            str(bot_self_id).strip(),
            str(group_id).strip(),
            str(target_user_id).strip(),
            str(action).strip().upper(),
            old_state,
            new_state,
            request_id,
            details_json,
        )

        await self.db.execute(sql, params)

    async def query(
        self, bot_self_id: str, *, target_user_id: str = "", limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Read recent audit entries for the selected Bot and optional user."""
        sql = "SELECT * FROM audit_log WHERE bot_self_id = ?"
        params = (bot_self_id,)
        if target_user_id:
            sql += " AND target_user_id = ?"
            params += (target_user_id,)
        rows = await self.db.fetchall(sql + " ORDER BY timestamp DESC, id DESC LIMIT ?", (*params, limit))
        return [dict(row) for row in rows]

    async def cleanup_old_logs(self, keep_days: int = 90) -> int:
        """Purge logs older than retention days."""
        cutoff = int(time.time()) - (keep_days * 86400)
        cursor = await self.db.execute("DELETE FROM audit_log WHERE timestamp < ?", (cutoff,))
        return cursor.rowcount
