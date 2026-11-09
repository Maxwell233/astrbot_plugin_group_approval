"""Persistent notification outbox and background retry worker.

Privacy model (see docs/SECURITY.md "Notification privacy"):

* **Administrator notifications** (new application) go to Owner/Reviewer private
  messages. On delivery failure they may fall back to an explicitly configured
  *dedicated review group* (``notifications.fallback_group_id``) - never to the group
  the application came from.
* **Applicant notifications** (approval result) go to the applicant's private message
  and contain the review note. If private delivery fails they are marked FAILED by
  default. A group fallback is possible only when
  ``notifications.user_group_fallback_enabled`` is explicitly enabled, and even then
  only a short, non-sensitive reminder is sent (``fallback_payload``), never the review
  reason or internal note.

Idempotency: ``event_key`` is a stable business event identifier (application id), so a
retried approval/rejection cannot enqueue duplicate outbox rows. Delivery itself is
at-least-once (QQ/NapCat offers no exactly-once primitive); outbox rows are claimed
atomically so two worker invocations cannot both send the same row concurrently.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

from .database import Database
from .napcat_client import NapCatClient

logger = logging.getLogger("astrbot_plugin_group_approval.notification")

# A row claimed by a worker but not updated for this long is considered abandoned
# (crash/restart) and may be reclaimed. Delivery may be retried at-least-once.
STALE_CLAIM_SECONDS = 300

DEFAULT_USER_FALLBACK_TEXT = "【权限通知】您有一条 Bot 使用权限申请的处理结果，请私聊 Bot 查看。"


class NotificationService:
    """Manages transactional outbox notifications and delivers them via NapCat."""

    def __init__(
        self,
        db: Database,
        client: NapCatClient,
        max_retries: int = 5,
        retry_interval_seconds: int = 60,
        user_group_fallback_enabled: bool = False,
        user_group_fallback_text: str = "",
    ):
        self.db = db
        self.client = client
        self.max_retries = max_retries
        self.retry_interval_seconds = retry_interval_seconds
        self.user_group_fallback_enabled = user_group_fallback_enabled
        self.user_group_fallback_text = user_group_fallback_text or DEFAULT_USER_FALLBACK_TEXT
        self._worker_task: asyncio.Task | None = None
        self._running = False

    async def enqueue(
        self,
        event_key: str,
        recipient_id: str,
        channel: str,
        payload: str,
        bot_self_id: str = "",
        fallback_group: str = "",
        fallback_payload: str = "",
    ) -> int:
        """Enqueue a message into the persistent outbox table with deduplication and bot routing.

        ``fallback_payload`` is the ONLY text that may be sent to a group when private
        delivery fails. Leave it empty to disable group fallback for this row entirely.
        """
        now = int(time.time())
        sql = """
        INSERT INTO notification_outbox (
            event_key, recipient_id, bot_self_id, fallback_group, fallback_payload,
            channel, payload, status, attempts, next_retry_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', 0, ?, ?)
        ON CONFLICT(event_key) DO NOTHING
        """
        params = (
            str(event_key).strip(),
            str(recipient_id).strip(),
            str(bot_self_id).strip(),
            str(fallback_group).strip(),
            str(fallback_payload),
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
        bot_self_id: str = "",
        fallback_group: str = "",
    ) -> None:
        """Enqueue application notices for reviewer private messages.

        ``fallback_group`` must be the dedicated administrator review group configured by
        the owner; it is never automatically set to the application's source group.
        """
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
                    bot_self_id=bot_self_id,
                    fallback_group=fallback_group,
                    # The dedicated review group is a trusted operator channel, so the
                    # full request detail is acceptable there.
                    fallback_payload=msg if fallback_group else "",
                )

        # No private recipient configured at all: use the dedicated review group directly
        # when the owner configured one.
        if fallback_group and not recipients:
            await self.enqueue(
                event_key=f"APP_NOTIF_{app_id}_GROUP_{fallback_group}",
                recipient_id=str(fallback_group).strip(),
                channel="group",
                payload=msg,
                bot_self_id=bot_self_id,
            )

    async def notify_user_result(
        self,
        user_id: str,
        group_id: str,
        approved: bool,
        note: str = "",
        bot_self_id: str = "",
        event_id: str = "",
    ) -> None:
        """Notify the applicant of an approval result without leaking it to the group.

        ``event_id`` is the stable business event id (the application id). It makes the
        outbox insert idempotent; without it a random UUID is used, which preserves
        uniqueness but cannot deduplicate a retried business event.
        """
        if approved:
            msg = (
                f"【Bot 使用权限审批通过】\n"
                f"您在群 {group_id} 提交的 Bot 使用权限申请已获管理员批准！\n"
                f"您现在可以正常使用群内 Bot 的 AI 与插件功能。"
            )
            if note:
                msg += f"\n审批备注：{note}"
            kind = "APPROVED"
        else:
            msg = (
                f"【Bot 使用权限申请被拒绝】\n"
                f"您在群 {group_id} 提交的 Bot 使用权限申请未通过审批。\n"
                f"原因：{note or '未说明'}"
            )
            kind = "REJECTED"

        stable_id = str(event_id).strip() or uuid.uuid4().hex
        event_key = f"USER_NOTIF_{kind}_{stable_id}"

        # Privacy: the applicant's group is NOT used as a fallback destination. Only a
        # short non-sensitive reminder may be sent there, and only if the owner opted in.
        fallback_group = ""
        fallback_payload = ""
        if self.user_group_fallback_enabled and group_id:
            fallback_group = str(group_id).strip()
            fallback_payload = self.user_group_fallback_text

        await self.enqueue(
            event_key=event_key,
            recipient_id=user_id,
            channel="private",
            payload=msg,
            bot_self_id=bot_self_id,
            fallback_group=fallback_group,
            fallback_payload=fallback_payload,
        )

    # ------------------------------------------------------------------
    # Outbox worker
    # ------------------------------------------------------------------
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
                SELECT id, event_key, recipient_id, bot_self_id, fallback_group,
                       fallback_payload, channel, payload, attempts
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
            fallback_group = str(r["fallback_group"] or "").strip()
            fallback_payload = str(r["fallback_payload"] or "")
            channel = r["channel"]
            payload = r["payload"]
            attempts = r["attempts"] + 1

            success = False
            last_error = ""
            target_bot = bot_self_id if bot_self_id else None

            try:
                if channel == "private":
                    ok, _data, err_msg, retcode = await self.client.send_private_msg(
                        recipient, payload, bot_self_id=target_bot
                    )
                else:
                    ok, _data, err_msg, retcode = await self.client.send_group_msg(
                        recipient, payload, bot_self_id=target_bot
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
                if channel == "private" and fallback_group and fallback_payload:
                    logger.warning(
                        "Private notification %s failed after %s attempts; sending minimal fallback",
                        outbox_id,
                        attempts,
                    )
                    try:
                        fb_ok, _fb_data, fb_err, _ = await self.client.send_group_msg(
                            group_id=fallback_group,
                            message=fallback_payload,
                            bot_self_id=target_bot,
                        )
                        if fb_ok:
                            success = True
                            last_error = f"{last_error} (Delivered via fallback group {fallback_group})"
                        else:
                            last_error = f"{last_error} (fallback failed: {fb_err})"
                    except Exception as fb_exc:  # noqa: BLE001
                        last_error = f"{last_error} (fallback exception: {fb_exc})"

                await self.db.execute(
                    "UPDATE notification_outbox SET status = ?, attempts = ?, sent_at = ?, last_error = ? WHERE id = ?",
                    (
                        "SENT" if success else "FAILED",
                        attempts,
                        int(time.time()) if success else None,
                        last_error,
                        outbox_id,
                    ),
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
            except Exception as e:  # noqa: BLE001 - keep the worker alive
                logger.error(f"Error processing notification outbox: {e}")
            await asyncio.sleep(5.0)
