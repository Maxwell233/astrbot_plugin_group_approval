"""QQ reviewer membership and persistent Owner overrides."""

from __future__ import annotations

import time

from .config import PluginConfig
from .database import Database


class ReviewerService:
    def __init__(self, db: Database, config: PluginConfig):
        self.db = db
        self.config = config

    async def can_review(self, user: str) -> bool:
        if self.config.is_owner(user):
            return True
        row = await self.db.fetchone("SELECT action FROM reviewer_overrides WHERE qq_id = ?", (user,))
        return row["action"] == "ADDED" if row else user in self.config.reviewer_qq_ids

    async def list_reviewers(self) -> list[str]:
        rows = await self.db.fetchall("SELECT qq_id, action FROM reviewer_overrides")
        configured = set(self.config.reviewer_qq_ids)
        for row in rows:
            if row["action"] == "ADDED":
                configured.add(row["qq_id"])
            else:
                configured.discard(row["qq_id"])
        return sorted(configured)

    async def set_reviewer(self, user: str, actor: str, *, enabled: bool) -> str:
        action = "ADDED" if enabled else "REMOVED"
        await self.db.execute(
            """INSERT INTO reviewer_overrides (qq_id, action, changed_by, changed_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(qq_id) DO UPDATE SET action = excluded.action,
               changed_by = excluded.changed_by, changed_at = excluded.changed_at""",
            (user, action, actor, int(time.time())),
        )
        return f"已{'添加' if enabled else '移除'}审核员 {user}。"
