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
