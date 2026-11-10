"""Scoped Outbox privacy, retry semantics, and multi-Bot delivery."""

import asyncio

import pytest

from approval.notification_service import NotificationService
from tests.test_scope_service import BOT, OTHER_BOT, PRIMARY, REVIEWER, USER

SECRET_NOTE = "test private note"


class RecordingClient:
    def __init__(self, private_ok=False):
        self.private_ok = private_ok
        self.private_calls = []
        self.group_calls = []

    async def send_private_msg(self, user_id, message, bot_self_id=None):
        self.private_calls.append((user_id, message, bot_self_id))
        if self.private_ok:
            return True, {"message_id": 1}, "", 0
        return False, None, "private message rejected", 100

    async def send_group_msg(self, *args, **kwargs):
        self.group_calls.append((args, kwargs))
        raise AssertionError("review notifications are private")


async def test_applicant_notification_never_leaks_to_group_by_default(temp_db):
    client = RecordingClient()
    service = NotificationService(temp_db, client, max_retries=1)
    await service.enqueue("result", USER, SECRET_NOTE, bot_self_id=BOT)
    assert await service.process_outbox_once() == 1
    assert client.private_calls == [(USER, SECRET_NOTE, BOT)]
    assert not client.group_calls
    row = await temp_db.fetchone("SELECT status, last_error FROM notification_outbox")
    assert row["status"] == "FAILED"
    assert "private message rejected" in row["last_error"]


async def test_user_notification_is_idempotent_for_same_business_event(temp_db):
    client = RecordingClient(True)
    service = NotificationService(temp_db, client)
    for _ in range(3):
        await service.enqueue("result", USER, SECRET_NOTE, bot_self_id=BOT)
    assert await service.process_outbox_once() == 1
    assert client.private_calls == [(USER, SECRET_NOTE, BOT)]


async def test_concurrent_outbox_workers_do_not_double_send(temp_db):
    client = RecordingClient(True)
    a = NotificationService(temp_db, client)
    b = NotificationService(temp_db, client)
    await a.enqueue("result", USER, SECRET_NOTE, bot_self_id=BOT)
    assert sum(await asyncio.gather(a.process_outbox_once(), b.process_outbox_once())) == 1
    assert len(client.private_calls) == 1


async def test_private_success_sends_exactly_one_message_with_note(temp_db):
    client = RecordingClient(True)
    service = NotificationService(temp_db, client)
    await service.enqueue("result", USER, SECRET_NOTE, bot_self_id=BOT)
    assert await service.process_outbox_once() == 1
    assert await service.process_outbox_once() == 0
    assert client.private_calls == [(USER, SECRET_NOTE, BOT)]
    row = await temp_db.fetchone("SELECT status, attempts, sent_at FROM notification_outbox")
    assert row["status"] == "SENT" and row["attempts"] == 1 and row["sent_at"]


@pytest.mark.parametrize("identity", ["", "default"])
async def test_outbox_without_verified_identity_never_uses_arbitrary_bot(temp_db, identity):
    client = RecordingClient(True)
    service = NotificationService(temp_db, client)
    await service.enqueue("result", USER, SECRET_NOTE, bot_self_id=identity)
    await service.process_outbox_once()
    assert not client.private_calls and not client.group_calls
    assert (await temp_db.fetchone("SELECT status FROM notification_outbox"))["status"] == "FAILED"


async def test_worker_retries_private_messages_per_bot(temp_db):
    client = RecordingClient()
    service = NotificationService(temp_db, client, max_retries=2)
    await service.enqueue("result-a", USER, SECRET_NOTE, bot_self_id=BOT)
    await service.enqueue("result-b", USER, SECRET_NOTE, bot_self_id=OTHER_BOT)
    await service.process_outbox_once()
    assert {r["status"] for r in await temp_db.fetchall("SELECT status FROM notification_outbox")} == {"PENDING"}
    await temp_db.execute("UPDATE notification_outbox SET next_retry_at = 0")
    client.private_ok = True
    await service.process_outbox_once()
    assert [bot for _, _, bot in client.private_calls] == [BOT, OTHER_BOT, BOT, OTHER_BOT]
    assert not client.group_calls


async def test_review_and_result_notifications_use_private_recipients(scoped):
    service, napcat = scoped
    napcat.set_member(PRIMARY, USER, "25")
    await service.apply(BOT, USER, "PRIVATE", "10002", SECRET_NOTE)
    app, = (await service.overview(BOT))["applications"]
    await service.review(BOT, app["id"], REVIEWER, "REVIEWER", approve=False, note=SECRET_NOTE)
    client = RecordingClient()
    await NotificationService(service.db, client, max_retries=1).process_outbox_once()
    assert {recipient for recipient, _, _ in client.private_calls} == {USER, REVIEWER, "99999"}
    assert any(recipient == USER and SECRET_NOTE in text for recipient, text, _ in client.private_calls)
    assert all(bot == BOT for _, _, bot in client.private_calls)
    assert not client.group_calls
