"""Regression tests for notification privacy, idempotency and worker concurrency.

These use the real ``NotificationService`` + real SQLite outbox; only the NapCat network
client is a stand-in, because no QQ/NapCat is available here.
"""

from __future__ import annotations

import asyncio

import pytest

from approval.database import Database
from approval.notification_service import NotificationService

SECRET_NOTE = "内部备注-SECRET-请勿外泄"


class RecordingClient:
    """Records sends and can fail private delivery on demand."""

    def __init__(self, private_ok: bool = False, delay: float = 0.0) -> None:
        self.private_ok = private_ok
        self.delay = delay
        self.private_calls: list[tuple[str, str]] = []
        self.group_calls: list[tuple[str, str]] = []

    async def send_private_msg(self, user_id, message, bot_self_id=None, **kwargs):
        if self.delay:
            await asyncio.sleep(self.delay)
        self.private_calls.append((str(user_id), message))
        if self.private_ok:
            return True, {"message_id": 1}, "", 0
        return False, None, "private message rejected", 100

    async def send_group_msg(self, group_id, message, bot_self_id=None, **kwargs):
        if self.delay:
            await asyncio.sleep(self.delay)
        self.group_calls.append((str(group_id), message))
        return True, {"message_id": 2}, "", 0


@pytest.fixture
async def outbox(tmp_path):
    db = Database(tmp_path / "notif.db")
    await db.init()
    try:
        yield db
    finally:
        await db.close()


async def test_applicant_notification_never_leaks_to_group_by_default(outbox):
    client = RecordingClient(private_ok=False)
    svc = NotificationService(outbox, client, max_retries=1, retry_interval_seconds=1)

    await svc.notify_user_result(
        user_id="30005",
        group_id="10001",  # the applicant's public group
        approved=False,
        note=SECRET_NOTE,
        bot_self_id="bot1",
        event_id="A1001",
    )
    await svc.process_outbox_once()

    assert client.group_calls == [], "rejection reason leaked to the public group"
    row = await outbox.fetchone(
        "SELECT status, fallback_group, last_error FROM notification_outbox WHERE event_key = ?",
        ("USER_NOTIF_REJECTED_A1001",),
    )
    assert row["status"] == "FAILED"
    assert row["fallback_group"] == ""
    assert "private message rejected" in row["last_error"]


async def test_opt_in_group_fallback_sends_only_minimal_text(outbox):
    client = RecordingClient(private_ok=False)
    svc = NotificationService(
        outbox,
        client,
        max_retries=1,
        retry_interval_seconds=1,
        user_group_fallback_enabled=True,
        user_group_fallback_text="【权限通知】请私聊 Bot 查看结果。",
    )

    await svc.notify_user_result(
        user_id="30005", group_id="10001", approved=False, note=SECRET_NOTE,
        bot_self_id="bot1", event_id="A1002",
    )
    await svc.process_outbox_once()

    assert len(client.group_calls) == 1
    target, text = client.group_calls[0]
    assert target == "10001"
    assert text == "【权限通知】请私聊 Bot 查看结果。"
    assert SECRET_NOTE not in text, "review note leaked through fallback payload"
    row = await outbox.fetchone(
        "SELECT status FROM notification_outbox WHERE event_key = ?", ("USER_NOTIF_REJECTED_A1002",)
    )
    assert row["status"] == "SENT"


async def test_user_notification_is_idempotent_for_same_business_event(outbox):
    client = RecordingClient(private_ok=True)
    svc = NotificationService(outbox, client, max_retries=1)

    for _ in range(3):
        await svc.notify_user_result(
            user_id="30005", group_id="10001", approved=True, note="ok",
            bot_self_id="bot1", event_id="A1003",
        )

    rows = await outbox.fetchall(
        "SELECT id FROM notification_outbox WHERE event_key = ?", ("USER_NOTIF_APPROVED_A1003",)
    )
    assert len(rows) == 1, "duplicate outbox rows for the same business event"


async def test_admin_fallback_uses_configured_review_group_not_source_group(outbox):
    client = RecordingClient(private_ok=False)
    svc = NotificationService(outbox, client, max_retries=1, retry_interval_seconds=1)

    await svc.notify_admins_of_application(
        app_id="A2001",
        user_id="30005",
        group_id="10001",          # source group: must never receive the admin notice
        level=12,
        min_level=10,
        recipients=["88888"],
        bot_self_id="bot1",
        fallback_group="77777",    # dedicated administrator review group
    )
    await svc.process_outbox_once()

    assert len(client.group_calls) == 1
    target, _text = client.group_calls[0]
    assert target == "77777", f"admin fallback sent to {target}, expected the review group"


async def test_concurrent_outbox_workers_do_not_double_send(outbox):
    client = RecordingClient(private_ok=True, delay=0.001)
    svc = NotificationService(outbox, client, max_retries=3)
    for i in range(10):
        await svc.notify_user_result(
            user_id=f"30{i:03d}", group_id="10001", approved=True, note="",
            bot_self_id="bot1", event_id=f"A30{i:02d}",
        )

    # Two worker batches running concurrently must claim disjoint rows.
    await asyncio.gather(svc.process_outbox_once(), svc.process_outbox_once())

    recipients = [r for r, _ in client.private_calls]
    assert len(recipients) == 10, f"expected 10 sends, got {len(recipients)} (duplicate delivery)"
    assert len(set(recipients)) == 10


async def test_private_success_sends_exactly_one_message_with_note(outbox):
    client = RecordingClient(private_ok=True)
    svc = NotificationService(outbox, client, max_retries=1)

    await svc.notify_user_result(
        user_id="30005", group_id="10001", approved=True, note="审批备注A",
        bot_self_id="bot1", event_id="A1004",
    )
    await svc.process_outbox_once()

    assert len(client.private_calls) == 1
    assert "审批备注A" in client.private_calls[0][1]
    assert client.group_calls == []


async def test_legacy_outbox_without_bot_never_sends_via_an_arbitrary_bot(outbox):
    client = RecordingClient(private_ok=True)
    svc = NotificationService(outbox, client, max_retries=1)
    await svc.enqueue("legacy", "30005", "private", SECRET_NOTE)
    await svc.process_outbox_once()
    assert client.private_calls == client.group_calls == []
    assert (await outbox.fetchone("SELECT status FROM notification_outbox"))[0] == "FAILED"


async def test_review_fallback_must_never_be_the_applicants_group(outbox):
    client = RecordingClient(private_ok=False)
    svc = NotificationService(outbox, client, max_retries=1)
    for app, recipients in (("A1001", []), ("A1002", ["20001"])):
        await svc.notify_admins_of_application(
            app, "30005", "10001", 20, 10, recipients,
            bot_self_id="10002", fallback_group="10001",
        )
    await svc.process_outbox_once()
    assert client.group_calls == []
