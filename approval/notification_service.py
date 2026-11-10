"""Private review and approval notifications with a transactional retry outbox.

Stable event keys deduplicate enqueue. Delivery is at-least-once after a worker crash.
"""

from __future__ import annotations

import asyncio
import time

from astrbot.api import logger

from .database import Database
from .napcat_client import NapCatClient

# A row claimed by a worker but not updated for this long is considered abandoned
# (crash/restart) and may be reclaimed. Delivery may be retried at-least-once.
STALE_CLAIM_SECONDS = 300


class NotificationService:
    """Manages transactional outbox notifications and delivers them via NapCat."""

    def __init__(
        self,
        db: Database,
        client: NapCatClient,
        max_retries: int = 5,
        retry_interval_seconds: int = 60,
    ):
        self.db = db
        self.client = client
        self.max_retries = max_retries
        self.retry_interval_seconds = retry_interval_seconds
        self._worker_task: asyncio.Task | None = None
        self._running = False

    async def enqueue(self, event_key: str, recipient_id: str, payload: str, *, bot_self_id: str) -> None:
        """Enqueue one private message using the transaction owned by the caller."""
        now = int(time.time())
        await self.db.execute(
            """INSERT INTO notification_outbox (event_key, recipient_id, bot_self_id, payload, next_retry_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(event_key) DO NOTHING""",
            (event_key, recipient_id, bot_self_id, payload, now, now),
        )

    async def _claim_batch(self, limit: int = 20) -> list:
        """Atomically claim a batch of due outbox rows (status PENDING -> SENDING)."""
        now = int(time.time())
        async with self.db.transaction():
            # Recover rows abandoned by a crashed worker (at-least-once delivery).
            await self.db.execute(
                """
                UPDATE notification_outbox
                SET status = 'PENDING', next_retry_at = ?
                WHERE status = 'SENDING' AND claimed_at > 0 AND claimed_at < ?
                """,
                (now, now - STALE_CLAIM_SECONDS),
            )
            rows = await self.db.fetchall(
                """
                SELECT id FROM notification_outbox
                WHERE status = 'PENDING' AND next_retry_at <= ?
                ORDER BY id ASC LIMIT ?
                """,
                (now, limit),
            )
            ids = [r["id"] for r in rows]
            if not ids:
                return []
            placeholders = ",".join("?" for _ in ids)
            await self.db.execute(
                f"""
                UPDATE notification_outbox
                SET status = 'SENDING', claimed_at = ?
                WHERE id IN ({placeholders}) AND status = 'PENDING'
                """,
                (now, *ids),
            )
            # Only rows this transaction actually claimed are processed.
            claimed = await self.db.fetchall(
                f"""
                SELECT id, recipient_id, bot_self_id, payload, attempts
                FROM notification_outbox
                WHERE id IN ({placeholders}) AND status = 'SENDING' AND claimed_at = ?
                ORDER BY id ASC
                """,
                (*ids, now),
            )
            return claimed

    async def process_outbox_once(self) -> int:
        """Process one claimed batch of pending notifications."""
        rows = await self._claim_batch()
        processed = 0

        for r in rows:
            outbox_id = r["id"]
            recipient = r["recipient_id"]
            bot_self_id = str(r["bot_self_id"] or "").strip()
            payload = r["payload"]
            attempts = r["attempts"] + 1

            if not bot_self_id or bot_self_id.lower() == "default":
                await self.db.execute(
                    "UPDATE notification_outbox SET status = 'FAILED', last_error = ? WHERE id = ?",
                    ("Missing verified Bot identity on outbox row", outbox_id),
                )
                processed += 1
                continue

            success = False
            last_error = ""

            try:
                ok, _data, err_msg, retcode = await self.client.send_private_msg(
                    recipient, payload, bot_self_id=bot_self_id
                )
                if ok:
                    success = True
                else:
                    last_error = f"{err_msg} (retcode={retcode})"
            except Exception as e:  # noqa: BLE001 - delivery must never kill the worker
                last_error = f"{type(e).__name__}: {e}"

            if success:
                await self.db.execute(
                    "UPDATE notification_outbox SET status = 'SENT', attempts = ?, sent_at = ? WHERE id = ?",
                    (attempts, int(time.time()), outbox_id),
                )
            elif attempts >= self.max_retries:
                await self.db.execute(
                    "UPDATE notification_outbox SET status = 'FAILED', attempts = ?, last_error = ? WHERE id = ?",
                    (attempts, last_error, outbox_id),
                )
            else:
                backoff = self.retry_interval_seconds * (2 ** (attempts - 1))
                next_retry = int(time.time()) + backoff
                await self.db.execute(
                    """
                    UPDATE notification_outbox
                    SET status = 'PENDING', attempts = ?, next_retry_at = ?, last_error = ?
                    WHERE id = ?
                    """,
                    (attempts, next_retry, last_error, outbox_id),
                )
            processed += 1

        return processed

    def start_worker(self) -> None:
        """Start the background retry loop."""
        if self._worker_task is None or self._worker_task.done():
            self._running = True
            self._worker_task = asyncio.create_task(self._run_loop())

    async def stop_worker(self) -> None:
        """Stop background worker cleanly."""
        self._running = False
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self.process_outbox_once()
            except Exception as e:  # noqa: BLE001 - keep the worker alive
                logger.error(f"Error processing notification outbox: {e}")
            await asyncio.sleep(5.0)
