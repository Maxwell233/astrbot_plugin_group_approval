"""Tests for application submission, review, expiration, and status transitions."""

from __future__ import annotations

import time

import pytest

from approval.approval_service import ApprovalService
from approval.audit_service import AuditService
from approval.config import PluginConfig
from approval.database import Database
from approval.level_service import LevelService
from approval.models import ApplicationStatus, GrantStatus
from approval.notification_service import NotificationService
from tests.conftest import MockNapCatClient


@pytest.fixture
def approval_system(temp_db: Database, mock_napcat: MockNapCatClient, test_config: PluginConfig):
    audit = AuditService(temp_db)
    notif = NotificationService(temp_db, mock_napcat)
    lvl = LevelService(mock_napcat)
    approval = ApprovalService(
        db=temp_db,
        level_service=lvl,
        audit_service=audit,
        notification_service=notif,
        config=test_config,
    )
    return approval, temp_db, mock_napcat, test_config


@pytest.mark.asyncio
async def test_apply_qualifying_level_creates_pending(approval_system):
    approval, db, napcat, config = approval_system

    # 11. Qualified user (LV10 >= min_level 10) submits application
    success, msg, app = await approval.apply_for_access(
        bot_self_id="bot1",
        group_id="10001",
        user_id="20001",
        reason="想用Bot查今日运势",
    )
    assert success is True
    assert app is not None
    assert app.status == ApplicationStatus.PENDING
    assert app.level_at_application == 10
    assert "A" in app.id

    # Verify persistent state in SQLite
    row = await db.fetchone("SELECT * FROM applications WHERE id = ?", (app.id,))
    assert row is not None
    assert row["status"] == "PENDING"
    assert row["user_id"] == "20001"


@pytest.mark.asyncio
async def test_apply_under_level_rejected(approval_system):
    approval, db, napcat, config = approval_system

    # User 20002 has LV9, threshold is 10
    success, msg, app = await approval.apply_for_access(
        bot_self_id="bot1",
        group_id="10001",
        user_id="20002",
        reason="申请测试",
    )
    assert success is False
    assert "未达到申请要求" in msg
    assert app is None

    # Verify no PENDING record in database
    row = await db.fetchone("SELECT * FROM applications WHERE user_id = '20002'")
    assert row is None


@pytest.mark.asyncio
async def test_duplicate_pending_prevented(approval_system):
    approval, db, napcat, config = approval_system

    # 15. User submits first application
    success1, _, app1 = await approval.apply_for_access("bot1", "10001", "20001")
    assert success1 is True

    # User submits second application before first is approved
    success2, msg2, app2 = await approval.apply_for_access("bot1", "10001", "20001")
    assert success2 is False
    assert "已有待审核的申请" in msg2
    assert app2.id == app1.id


@pytest.mark.asyncio
async def test_reviewer_can_approve(approval_system):
    approval, db, napcat, config = approval_system

    # 12. Submit application
    _, _, app = await approval.apply_for_access("bot1", "10001", "20001")

    # Reviewer approves
    reviewer_id = "88888"
    success, msg = await approval.approve_application(
        actor_id=reviewer_id,
        actor_role="REVIEWER",
        application_id=app.id,
        note="准予授权",
    )
    assert success is True
    assert "已成功批准" in msg

    # Verify grant active
    grant = await approval.get_active_grant("bot1", "10001", "20001")
    assert grant is not None
    assert grant.status == GrantStatus.ACTIVE
    assert grant.approved_by == reviewer_id


@pytest.mark.asyncio
async def test_reviewer_can_reject(approval_system):
    approval, db, napcat, config = approval_system

    # 13. Submit application
    _, _, app = await approval.apply_for_access("bot1", "10001", "20001")

    # Reviewer rejects
    reviewer_id = "88888"
    success, msg = await approval.reject_application(
        actor_id=reviewer_id,
        actor_role="REVIEWER",
        application_id=app.id,
        reason="发言违规记录过多",
    )
    assert success is True

    # Verify application status REJECTED
    row = await db.fetchone("SELECT status FROM applications WHERE id = ?", (app.id,))
    assert row["status"] == "REJECTED"

    # User in cooldown period cannot re-apply immediately
    success2, msg2, _ = await approval.apply_for_access("bot1", "10001", "20001")
    assert success2 is False
    assert "冷却中" in msg2


@pytest.mark.asyncio
async def test_expired_application_cannot_be_approved(approval_system):
    approval, db, napcat, config = approval_system

    # 17. Expired application cannot be approved
    _, _, app = await approval.apply_for_access("bot1", "10001", "20001")

    # Force expiration in database
    past_ts = int(time.time()) - 100
    await db.execute("UPDATE applications SET expires_at = ? WHERE id = ?", (past_ts, app.id))
    await db.commit()

    # Trigger clean
    await approval.clean_expired()

    # Attempt to approve
    success, msg = await approval.approve_application("88888", "REVIEWER", app.id)
    assert success is False
    assert "该申请已被处理" in msg


async def test_approval_expires_during_live_level_lookup(approval_system, monkeypatch):
    from types import SimpleNamespace

    from approval import approval_service

    approval, db, _napcat, _config = approval_system
    now = int(time.time())
    clock = SimpleNamespace(time=lambda: now)
    _, _, app = await approval.apply_for_access("bot1", "10001", "20001")
    await db.execute("UPDATE applications SET expires_at = ? WHERE id = ?", (now + 1, app.id))
    lookup = approval.level_service.get_group_member_level

    async def slow_lookup(**kwargs):
        nonlocal now
        result = await lookup(**kwargs)
        now += 1
        return result

    monkeypatch.setattr(approval_service, "time", clock)
    monkeypatch.setattr(approval.level_service, "get_group_member_level", slow_lookup)
    ok, message = await approval.approve_application("88888", "REVIEWER", app.id)
    assert not ok and "过期" in message
    assert (await db.fetchone("SELECT status FROM applications WHERE id = ?", (app.id,)))[0] == "EXPIRED"
    assert await db.fetchone("SELECT id FROM grants") is None


async def test_global_ban_revokes_grants_in_all_groups_for_that_bot(approval_system):
    approval, db, napcat, _config = approval_system
    policy = await approval.get_group_policy("bot1", "10002")
    policy.enabled = True
    await approval.save_group_policy(policy)
    napcat.set_member("10002", "20001", level="10")
    for bot, group in [("bot1", "10001"), ("bot1", "10002"), ("bot2", "10001")]:
        assert (await approval.grant_manually("99999", "OWNER", bot, group, "20001", is_level_exempt=True))[0]
    assert (await approval.ban_user("99999", "OWNER", "bot1", "ALL", "20001"))[0]
    assert (await approval.unban_user("99999", "OWNER", "bot1", "ALL", "20001"))[0]
    rows = await db.fetchall("SELECT bot_self_id, status FROM grants ORDER BY bot_self_id, group_id")
    assert [tuple(row) for row in rows] == [("bot1", "REVOKED"), ("bot1", "REVOKED"), ("bot2", "ACTIVE")]


async def test_blacklisted_applicant_cannot_be_approved(approval_system):
    approval, db, _napcat, _config = approval_system
    _, _, app = await approval.apply_for_access("bot1", "10001", "20001")
    assert (await approval.ban_user("99999", "OWNER", "bot1", "ALL", "20001"))[0]
    ok, message = await approval.approve_application("88888", "REVIEWER", app.id)
    assert not ok and "黑名单" in message
    assert await db.fetchone("SELECT id FROM grants") is None


@pytest.mark.parametrize(("exempt", "level", "is_member", "query_fails", "allowed"), [
    (False, "18", True, False, True),
    (False, "9", True, False, False),
    (False, None, True, False, False),
    (False, "18", False, False, False),
    (False, "18", True, True, False),
    (True, "5", True, False, True),
    (True, "18", False, False, False),
    (True, "18", True, True, False),
])
async def test_manual_grant_requires_fresh_membership_and_level(
    approval_system, exempt, level, is_member, query_fails, allowed
):
    approval, db, napcat, _config = approval_system
    cached = await approval.level_service.get_group_member_level("bot1", "10001", "20001")
    assert cached.success
    napcat.set_member("10001", "20001", level=level, is_member=is_member)
    napcat.network_error_on_next = query_fails
    before = int(time.time())

    ok, message = await approval.grant_manually(
        "99999", "OWNER", "bot1", "10001", "20001", is_level_exempt=exempt,
    )
    assert ok is allowed, message
    grant = await approval.get_active_grant("bot1", "10001", "20001")
    audit = await db.fetchone("SELECT id FROM audit_log WHERE action = 'MANUAL_GRANT'")
    if allowed:
        assert grant.status == GrantStatus.ACTIVE
        assert grant.last_verified_level == int(level)
        assert grant.last_verified_at >= before
        assert grant.is_level_exempt == exempt
        assert audit is not None
    else:
        assert grant is None
        assert audit is None


@pytest.mark.parametrize("scope", ["10001", "ALL"])
async def test_unban_rolls_back_when_audit_insert_fails(approval_system, scope):
    from sqlite3 import IntegrityError

    approval, db, _napcat, _config = approval_system
    assert (await approval.ban_user("99999", "OWNER", "bot1", scope, "20001"))[0]
    await db.execute("""
        CREATE TRIGGER fail_unban_audit BEFORE INSERT ON audit_log
        WHEN NEW.action = 'USER_UNBANNED'
        BEGIN SELECT RAISE(ABORT, 'unban audit failed'); END
    """)
    with pytest.raises(IntegrityError, match="unban audit failed"):
        await approval.unban_user("99999", "OWNER", "bot1", scope, "20001")
    assert (await approval.is_blacklisted("bot1", "10001", "20001"))[0]
    assert await db.fetchone("SELECT id FROM audit_log WHERE action = 'USER_UNBANNED'") is None

    await db.execute("DROP TRIGGER fail_unban_audit")
    assert (await approval.unban_user("99999", "OWNER", "bot1", scope, "20001"))[0]
    assert not (await approval.is_blacklisted("bot1", "10001", "20001"))[0]
    assert (await db.fetchone("SELECT group_id FROM audit_log WHERE action = 'USER_UNBANNED'"))[0] == scope


async def test_approval_uses_enabled_policy_at_commit_and_rechecks_after_reenable(approval_system, monkeypatch):
    approval, db, napcat, _config = approval_system
    _, _, app = await approval.apply_for_access("bot1", "10001", "20001")
    policy = await approval.get_group_policy("bot1", "10001")
    lookup = approval.level_service.get_group_member_level

    async def disable_during_lookup(**kwargs):
        result = await lookup(**kwargs)
        policy.enabled = False
        await approval.save_group_policy(policy)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(approval.level_service, "get_group_member_level", disable_during_lookup)
        ok, message = await approval.approve_application("88888", "REVIEWER", app.id)
    assert not ok and "禁用" in message
    assert (await db.fetchone("SELECT status FROM applications WHERE id = ?", (app.id,)))[0] == "PENDING"
    assert await approval.get_active_grant("bot1", "10001", "20001") is None
    assert await db.fetchone("SELECT id FROM audit_log WHERE action = 'APPLICATION_APPROVED'") is None

    policy.enabled = True
    policy.min_level = 15
    await approval.save_group_policy(policy)
    ok, message = await approval.approve_application("88888", "REVIEWER", app.id)
    assert not ok and "LV15" in message

    napcat.set_member("10001", "20001", level="15")
    ok, message = await approval.approve_application("88888", "REVIEWER", app.id)
    assert ok, message
    assert (await approval.get_active_grant("bot1", "10001", "20001")).last_verified_level == 15


async def test_manual_grant_cannot_create_active_grant_for_disabled_group(approval_system):
    approval, _db, _napcat, _config = approval_system
    policy = await approval.get_group_policy("bot1", "10001")
    policy.enabled = False
    await approval.save_group_policy(policy)
    ok, message = await approval.grant_manually("99999", "OWNER", "bot1", "10001", "20001")
    assert not ok and "禁用" in message
    assert await approval.get_active_grant("bot1", "10001", "20001") is None


async def test_manual_grant_does_not_join_an_inflight_cached_member_lookup(approval_system, monkeypatch):
    import asyncio

    approval, db, napcat, _config = approval_system
    cached_lookup_started = asyncio.Event()
    release_cached_lookup = asyncio.Event()
    cache_flags = []
    original = napcat.get_group_member_info

    async def lookup(*, no_cache, **kwargs):
        cache_flags.append(no_cache)
        if no_cache:
            return {"status": "failed", "retcode": 10001, "msg": "Member left group"}
        result = await original(no_cache=no_cache, **kwargs)
        cached_lookup_started.set()
        await release_cached_lookup.wait()
        return result

    monkeypatch.setattr(napcat, "get_group_member_info", lookup)
    cached_task = asyncio.create_task(
        approval.level_service.get_group_member_level("bot1", "10001", "20001")
    )
    try:
        await asyncio.wait_for(cached_lookup_started.wait(), 2)
        ok, message = await asyncio.wait_for(
            approval.grant_manually("99999", "OWNER", "bot1", "10001", "20001"), 2
        )
        assert not ok, message
        assert cache_flags == [False, True]
        assert await db.fetchone("SELECT id FROM grants") is None
    finally:
        release_cached_lookup.set()
        await cached_task


@pytest.mark.parametrize("reply", [
    {"user_id": 20001, "group_id": 10001, "level": "20", "is_member": False},
    {"group_id": 10001, "level": "20"},
    {"user_id": 20001, "level": "20"},
])
async def test_exempt_manual_grant_requires_reply_to_verify_target(approval_system, monkeypatch, reply):
    approval, db, napcat, _config = approval_system

    async def lookup(**kwargs):
        return reply

    monkeypatch.setattr(napcat, "get_group_member_info", lookup)
    ok, message = await approval.grant_manually(
        "99999", "OWNER", "bot1", "10001", "20001", is_level_exempt=True,
    )
    assert not ok, message
    assert await db.fetchone("SELECT id FROM grants") is None
