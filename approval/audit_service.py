"""Audit logging service for tracking administrative actions and status changes."""

from __future__ import annotations

import csv
import io
import json
import logging
import time
from typing import Any

from .database import Database
from .models import AuditLogEntry

logger = logging.getLogger("astrbot_plugin_group_approval.audit")


class AuditService:
    """Records security-critical events and provides query/export functions."""

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
    ) -> int:
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

        cursor = await self.db.execute(sql, params)
        await self.db.commit()
        return cursor.lastrowid or 0

    async def query(
        self,
        bot_self_id: str | None = None,
        group_id: str | None = None,
        target_user_id: str | None = None,
        actor_id: str | None = None,
        action: str | None = None,
        start_time: int | None = None,
        end_time: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AuditLogEntry]:
        """Query audit log entries with flexible filtering."""
        clauses = []
        params: list[Any] = []

        if bot_self_id:
            clauses.append("bot_self_id = ?")
            params.append(bot_self_id)
        if group_id:
            clauses.append("group_id = ?")
            params.append(group_id)
        if target_user_id:
            clauses.append("target_user_id = ?")
            params.append(target_user_id)
        if actor_id:
            clauses.append("actor_id = ?")
            params.append(actor_id)
        if action:
            clauses.append("action = ?")
            params.append(action.upper())
        if start_time is not None:
            clauses.append("timestamp >= ?")
            params.append(start_time)
        if end_time is not None:
            clauses.append("timestamp <= ?")
            params.append(end_time)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
        SELECT id, timestamp, actor_id, actor_type, bot_self_id, group_id,
               target_user_id, action, old_state, new_state, request_id, details_json
        FROM audit_log
        {where_sql}
        ORDER BY timestamp DESC, id DESC
        LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])

        rows = await self.db.fetchall(sql, tuple(params))
        entries = []
        for r in rows:
            entries.append(
                AuditLogEntry(
                    id=r["id"],
                    timestamp=r["timestamp"],
                    actor_id=r["actor_id"],
                    actor_type=r["actor_type"],
                    bot_self_id=r["bot_self_id"],
                    group_id=r["group_id"],
                    target_user_id=r["target_user_id"],
                    action=r["action"],
                    old_state=r["old_state"],
                    new_state=r["new_state"],
                    request_id=r["request_id"],
                    details_json=r["details_json"],
                )
            )
        return entries

    async def export_json(self, **kwargs: Any) -> str:
        entries = await self.query(**kwargs)
        return json.dumps([e.to_dict() for e in entries], ensure_ascii=False, indent=2)

    async def export_csv(self, **kwargs: Any) -> str:
        entries = await self.query(**kwargs)
        output = io.StringIO()
        fieldnames = [
            "id",
            "timestamp",
            "actor_id",
            "actor_type",
            "bot_self_id",
            "group_id",
            "target_user_id",
            "action",
            "old_state",
            "new_state",
            "request_id",
            "details_json",
        ]
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for e in entries:
            writer.writerow(e.to_dict())
        return output.getvalue()

    async def cleanup_old_logs(self, keep_days: int = 90) -> int:
        """Purge logs older than retention days."""
        cutoff = int(time.time()) - (keep_days * 86400)
        cursor = await self.db.execute("DELETE FROM audit_log WHERE timestamp < ?", (cutoff,))
        await self.db.commit()
        return cursor.rowcount
