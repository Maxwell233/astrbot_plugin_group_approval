"""Tests for concurrent approvals and race conditions."""

from __future__ import annotations

import asyncio

import pytest

from approval.approval_service import ApprovalService
from approval.audit_service import AuditService
from approval.config import PluginConfig
from approval.database import Database
from approval.level_service import LevelService
from approval.notification_service import NotificationService
from tests.conftest import MockNapCatClient


@pytest.mark.asyncio
async def test_concurrent_approval_atomic(temp_db: Database, mock_napcat: MockNapCatClient, test_config: PluginConfig):
    audit = AuditService(temp_db)
    notif = NotificationService(temp_db, mock_napcat)
    lvl = LevelService(mock_napcat)
    approval = ApprovalService(temp_db, lvl, audit, notif, test_config)

    # Submit application
    success, _, app = await approval.apply_for_access("bot1", "10001", "20001")
    assert success is True

    # 16. Two admins attempt to approve the same application simultaneously
    task1 = approval.approve_application("admin_1", "REVIEWER", app.id, "Admin 1 approve")
    task2 = approval.approve_application("admin_2", "REVIEWER", app.id, "Admin 2 approve")

    res1, res2 = await asyncio.gather(task1, task2)

    # Exactly one succeeded and one failed
    successes = [r for r in (res1, res2) if r[0] is True]
    failures = [r for r in (res1, res2) if r[0] is False]

    assert len(successes) == 1
    assert len(failures) == 1
    assert "已被处理" in failures[0][1]

    # Verify database has exactly 1 grant record
    rows = await temp_db.fetchall(
        "SELECT COUNT(*) FROM grants WHERE bot_self_id = 'bot1' AND group_id = '10001' AND user_id = '20001'"
    )
    assert rows[0][0] == 1


@pytest.mark.asyncio
async def test_concurrent_application_submission(temp_db: Database, mock_napcat: MockNapCatClient, test_config: PluginConfig):
    audit = AuditService(temp_db)
    notif = NotificationService(temp_db, mock_napcat)
    lvl = LevelService(mock_napcat)
    approval = ApprovalService(temp_db, lvl, audit, notif, test_config)

    # Same user launches 3 simultaneous apply requests
    t1 = approval.apply_for_access("bot1", "10001", "20001")
    t2 = approval.apply_for_access("bot1", "10001", "20001")
    t3 = approval.apply_for_access("bot1", "10001", "20001")

    results = await asyncio.gather(t1, t2, t3)

    # Exactly 1 application created
    successes = [r for r in results if r[0] is True]
    assert len(successes) == 1

    # Verify in DB only 1 pending application
    rows = await temp_db.fetchall(
        "SELECT COUNT(*) FROM applications WHERE bot_self_id = 'bot1' AND group_id = '10001' AND user_id = '20001' AND status = 'PENDING'"
    )
    assert rows[0][0] == 1
