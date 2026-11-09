"""Tests for v0.1.1 Round 2 security hardening, authentication, outbox fallback, and protocol fixes."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from approval.auth_service import AuthorizationService
from approval.diagnostics import HandlerDiagnostics
from approval.level_service import LevelService
from approval.models import GrantStatus
from approval.napcat_client import NapCatClient, normalize_onebot_response
from approval.notification_service import NotificationService


# 1. Dashboard authentication & authorization (identity separation)
@pytest.mark.asyncio
async def test_webui_auth_enforcement(access_system):
    access, approval, db, napcat, config = access_system
    # The AstrBot dashboard username is the only dashboard identity we trust.
    auth = AuthorizationService(config, dashboard_username_getter=lambda: "astrbot")

    class UnauthReq:
        pass

    class HeaderSpoofReq:
        headers = {"X-AstrBot-User": "astrbot"}

    class ApiKeyReq:
        username = "api_key:abc123"

    class DashboardReq:
        username = "astrbot"

    assert auth.get_authenticated_user(UnauthReq()) is None
    assert auth.get_authenticated_user(HeaderSpoofReq()) is None  # headers are not identity
    assert auth.get_authenticated_user(ApiKeyReq()) is None  # machine credential
    assert auth.get_authenticated_user(DashboardReq()) == "astrbot"

    # Only the configured dashboard username is OWNER. QQ ids and the old hardcoded
    # 'admin'/'root'/'owner' usernames are NEVER dashboard owners.
    assert await auth.get_user_role("astrbot") == "OWNER"
    for name in ("admin", "root", "owner", "99999", "88888", "reviewer_bob"):
        assert await auth.get_user_role(name) == "DASHBOARD_USER", name

    # A non-owner authenticated dashboard identity is read-only.
    ok_appr, role_appr, reason_appr = await auth.authorize_action("reviewer_bob", "approve")
    assert ok_appr is False
    assert role_appr == "DASHBOARD_USER"
    ok_read, _, _ = await auth.authorize_action("reviewer_bob", "list_applications")
    assert ok_read is True

    # QQ Reviewer status does not leak into dashboard privileges.
    await approval.add_reviewer("reviewer_bob")
    ok_rev, role_rev, _ = await auth.authorize_action("reviewer_bob", "approve")
    assert ok_rev is False
    assert role_rev == "DASHBOARD_USER"

    # Dashboard owner can perform mutations, including level-exempt grant.
    ok_owner, role_owner, _ = await auth.authorize_action("astrbot", "grant", is_level_exempt=True)
    assert ok_owner is True
    assert role_owner == "OWNER"


# 2. Post-Type Filtering: Notices & Requests are NEVER intercepted
def test_post_type_filtering_only_identifies_chat_messages():
    from main import _is_chat_message

    event = object()
    assert not _is_chat_message(event, {"post_type": "notice"})
    assert not _is_chat_message(event, {"post_type": "request"})
    assert _is_chat_message(event, {"post_type": "message"})


# 3. Strict Level Pattern Parsing
def test_strict_level_pattern_matching():
    level_service = LevelService(NapCatClient())

    # Valid patterns
    assert level_service.parse_level(18) == 18
    assert level_service.parse_level("18") == 18
    assert level_service.parse_level("LV18") == 18
    assert level_service.parse_level("lv 18") == 18
    assert level_service.parse_level("等级18") == 18
    assert level_service.parse_level("Lv0") == 0

    # Invalid / messy text patterns that MUST NOT be matched
    assert level_service.parse_level("rank_999_user") is None
    assert level_service.parse_level("VIP1234") is None
    assert level_service.parse_level("LV") is None
    assert level_service.parse_level("hello 18 world") is None
    assert level_service.parse_level("-5") is None
    assert level_service.parse_level("") is None
    assert level_service.parse_level(None) is None


# 4. Atomic Approval with Outbox and Audit Rollback
@pytest.mark.asyncio
async def test_atomic_approval_rollback_on_failure(access_system):
    access, approval, db, napcat, config = access_system
    napcat.set_member("10001", "30005", level="15")

    # Submit application
    ok, _, app = await approval.apply_for_access("bot1", "10001", "30005", "Need access")
    assert ok is True
    assert app is not None

    # Simulate database crash during outbox notification enqueue
    original_enqueue = approval.notifications.enqueue
    async def failing_enqueue(*args, **kwargs):
        raise RuntimeError("Simulated outbox disk failure")
    approval.notifications.enqueue = failing_enqueue

    # Attempt approval
    ok_appr, msg = await approval.approve_application(
        actor_id="99999",
        actor_role="OWNER",
        application_id=app.id,
    )
    assert ok_appr is False
    assert "数据库事务异常" in msg

    # Restore enqueue
    approval.notifications.enqueue = original_enqueue

    # Verify rollback: application is still PENDING, no grant exists
    row_app = await db.fetchone("SELECT status FROM applications WHERE id = ?", (app.id,))
    assert row_app["status"] == "PENDING"
    grant = await approval.get_active_grant("bot1", "10001", "30005")
    assert grant is None


# 5. Manual Grant UUID Uniqueness Concurrency
@pytest.mark.asyncio
async def test_manual_grant_uuid_uniqueness(access_system):
    access, approval, db, napcat, config = access_system

    # Concurrently grant access to 50 users
    tasks = []
    for i in range(50):
        uid = str(40000 + i)
        napcat.set_member("10001", uid, level="15")
        tasks.append(
            approval.grant_manually(
                actor_id="99999",
                actor_role="OWNER",
                bot_self_id="bot1",
                group_id="10001",
                user_id=uid,
                duration_days=7,
            )
        )
    results = await asyncio.gather(*tasks)
    assert all(r[0] for r in results)

    # Verify all 50 grant IDs are distinct
    rows = await db.fetchall("SELECT id FROM grants WHERE bot_self_id = 'bot1' AND group_id = '10001'")
    ids = [r["id"] for r in rows]
    assert len(ids) == 50
    assert len(set(ids)) == 50
    assert all(id_val.startswith("G_MANUAL_") for id_val in ids)


# 6. Notification Outbox Multi-bot Routing & Fallback State Machine
@pytest.mark.asyncio
async def test_outbox_multi_bot_and_group_fallback(temp_db):
    mock_client = MagicMock()
    # Private message fails
    mock_client.send_private_msg = AsyncMock(return_value=(False, None, "Risk control 100", 100))
    # Group message succeeds
    mock_client.send_group_msg = AsyncMock(return_value=(True, {"message_id": 888}, "", 0))

    notif_svc = NotificationService(
        db=temp_db,
        client=mock_client,
        max_retries=2,
        retry_interval_seconds=1,
    )

    # Enqueue a private notification with an explicitly supplied *minimal* fallback
    # payload. A fallback group alone must NOT re-send the full private payload to a
    # group (that was the old privacy leak; see SECURITY.md "Notification privacy").
    outbox_id = await notif_svc.enqueue(
        event_key="EVT_FALLBACK_TEST_1",
        recipient_id="40001",
        channel="private",
        payload="Test approval notification",
        bot_self_id="bot_qq_999",
        fallback_group="group_8888",
        fallback_payload="【权限通知】请私聊查看结果。",
    )
    assert outbox_id > 0

    # Process 1st attempt (fails, schedules retry)
    await notif_svc.process_outbox_once()
    row1 = await temp_db.fetchone("SELECT status, attempts, bot_self_id, fallback_group FROM notification_outbox WHERE id = ?", (outbox_id,))
    assert row1["status"] == "PENDING"
    assert row1["attempts"] == 1
    assert row1["bot_self_id"] == "bot_qq_999"
    assert row1["fallback_group"] == "group_8888"

    # Fast-forward time for retry
    await temp_db.execute("UPDATE notification_outbox SET next_retry_at = 0 WHERE id = ?", (outbox_id,))
    await temp_db.commit()

    # Process 2nd attempt (hits max_retries=2, triggers fallback group send)
    await notif_svc.process_outbox_once()
    row2 = await temp_db.fetchone("SELECT status, attempts, last_error FROM notification_outbox WHERE id = ?", (outbox_id,))
    assert row2["status"] == "SENT"
    assert row2["attempts"] == 2
    assert "Delivered via fallback group group_8888" in row2["last_error"]

    # Verify mock_client called send_group_msg with the minimal fallback payload only
    # (never the full private payload) and the correct bot_self_id.
    mock_client.send_group_msg.assert_called_with(
        group_id="group_8888",
        message="【权限通知】请私聊查看结果。",
        bot_self_id="bot_qq_999",
    )
