"""SQLite fixed-window limits shared by application lookup and creation."""

from __future__ import annotations

import time

from .database import Database


class RateLimiter:
    def __init__(self, db: Database):
        self.db = db

    async def allow(self, key: str, limit: int, window_seconds: int = 3600) -> bool:
        """Reserve one slot, within the caller's transaction when present."""
        async with self.db.transaction():
            now = int(time.time())
            cutoff = now - window_seconds
            cursor = await self.db.execute(
                """INSERT INTO rate_limits (key, count, window_start) VALUES (?, 1, ?)
                   ON CONFLICT(key) DO UPDATE SET
                     count = CASE WHEN window_start <= ? THEN 1 ELSE count + 1 END,
                     window_start = CASE WHEN window_start <= ? THEN excluded.window_start ELSE window_start END
                   WHERE window_start <= ? OR count < ?""",
                (key, now, cutoff, cutoff, cutoff, limit),
            )
            return cursor.rowcount == 1
