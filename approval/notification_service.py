"""Persistent notification outbox and background retry worker."""

from __future__ import annotations

import asyncio
import logging
import time

from .database import Database
from .napcat_client import NapCatClient

logger = logging.getLogger("astrbot_plugin_group_approval.notification")


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

    async def enqueue(
        self,
        event_key: str,
        recipient_id: str,
        channel: str,
        payload: str,
        fallback_group: str = "",
    ) -> int:
        """Enqueue a message into the persistent outbox table with deduplication."""
        now = int(time.time())
        sql = """
        INSERT INTO notification_outbox (
            event_key, recipient_id, channel, payload, status, attempts, next_retry_at, created_at
        ) VALUES (?, ?, ?, ?, 'PENDING', 0, ?, ?)
        ON CONFLICT(event_key) DO NOTHING
        """
        params = (
            str(event_key).strip(),
            str(recipient_id).strip(),
            str(channel).strip().lower(),
            payload,
            now,
            now,
        )
        cursor = await self.db.execute(sql, params)
        await self.db.commit()
        return cursor.lastrowid or 0

    async def notify_admins_of_application(
        self,
        app_id: str,
        user_id: str,
        group_id: str,
        level: int,
        min_level: int,
        recipients: list[str],
        fallback_group: str = "",
    ) -> None:
        """Enqueue application notice to reviewer private messages. Fallback group only used on failure."""
        msg = (
            f"【Bot 使用权限申请】\n\n"
            f"申请编号：{app_id}\n"
            f"QQ：{user_id}\n"
            f"群号：{group_id}\n"
            f"当前群等级：LV{level}\n"
            f"申请门槛：LV{min_level}\n"
            f"状态：等待审批\n\n"
            f"批准：\n/ga approve {app_id}\n\n"
            f"拒绝：\n/ga reject {app_id}"
        )

        for admin_qq in recipients:
            admin_qq = str(admin_qq).strip()
            if admin_qq:
                await self.enqueue(
                    event_key=f"APP_NOTIF_{app_id}_{admin_qq}",
                    recipient_id=admin_qq,
                    channel="private",
                    payload=msg,
                    fallback_group=fallback_group,
                )

        # Store fallback group association in case all private notifications fail
        if fallback_group and not recipients:
            # If no private admin configured at all, fallback directly
            await self.enqueue(
                event_key=f"APP_NOTIF_{app_id}_GROUP_{fallback_group}",
                recipient_id=str(fallback_group).strip(),
                channel="group",
                payload=msg,
            )

    async def notify_user_result(
        self,
        user_id: str,
        group_id: str,
        approved: bool,
        note: str = "",
    ) -> None:
        """Notify user of approval or rejection result."""
        if approved:
            msg = (
                f"【Bot 使用权限审批通过】\n"
                f"您在群 {group_id} 提交的 Bot 使用权限申请已获管理员批准！\n"
                f"您现在可以正常使用群内 Bot 的 AI 与插件功能。"
            )
            if note:
                msg += f"\n审批备注：{note}"
        else:
            msg = (
                f"【Bot 使用权限申请被拒绝】\n"
                f"您在群 {group_id} 提交的 Bot 使用权限申请未通过审批。\n"
                f"原因：{note or '未说明'}"
            )

        await self.enqueue(
            event_key=f"USER_NOTIF_{user_id}_{group_id}_{int(time.time())}",
            recipient_id=user_id,
            channel="private",
            payload=msg,
        )

    async def process_outbox_once(self) -> int:
        """Process one batch of pending notifications."""
        now = int(time.time())
        sql = """
        SELECT id, event_key, recipient_id, channel, payload, status, attempts, next_retry_at
        FROM notification_outbox
        WHERE status = 'PENDING' AND next_retry_at <= ?
        ORDER BY id ASC
        LIMIT 20
        """
        rows = await self.db.fetchall(sql, (now,))
        processed = 0

        for r in rows:
            outbox_id = r["id"]
            recipient = r["recipient_id"]
            channel = r["channel"]
            payload = r["payload"]
            attempts = r["attempts"] + 1

            # Dispatch notification
            success = False
            last_error = ""

            try:
                if channel == "private":
                    ok, data, err_msg, retcode = await self.client.send_private_msg(recipient, payload)
                else:
                    ok, data, err_msg, retcode = await self.client.send_group_msg(recipient, payload)

                if ok:
                    success = True
                else:
                    last_error = f"{err_msg} (retcode={retcode})"
            except Exception as e:
                last_error = str(e)

            if success:
                await self.db.execute(
                    "UPDATE notification_outbox SET status = 'SENT', attempts = ?, sent_at = ? WHERE id = ?",
                    (attempts, int(time.time()), outbox_id),
                )
            else:
                if attempts >= self.max_retries:
                    await self.db.execute(
                        "UPDATE notification_outbox SET status = 'FAILED', attempts = ?, last_error = ? WHERE id = ?",
                        (attempts, last_error, outbox_id),
                    )
                else:
                    backoff = self.retry_interval_seconds * (2 ** (attempts - 1))
                    next_retry = int(time.time()) + backoff
                    await self.db.execute(
                        "UPDATE notification_outbox SET attempts = ?, next_retry_at = ?, last_error = ? WHERE id = ?",
                        (attempts, next_retry, last_error, outbox_id),
                    )
            await self.db.commit()
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
            except Exception as e:
                logger.error(f"Error processing notification outbox: {e}")
            await asyncio.sleep(5.0)
