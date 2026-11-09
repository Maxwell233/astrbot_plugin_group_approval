"""Tests for access decision engine, revocation, blacklisting, and level rechecks."""

from __future__ import annotations

import time

import pytest

from approval.access_service import AccessService
from approval.approval_service import ApprovalService
from approval.audit_service import AuditService
from approval.config import PluginConfig
from approval.database import Database
from approval.level_service import LevelService
from approval.models import GrantStatus
from approval.notification_service import NotificationService
from tests.conftest import MockNapCatClient


@pytest.fixture
def access_system(temp_db: Database, mock_napcat: MockNapCatClient, test_config: PluginConfig):
    audit = AuditService(temp_db)
    notif = NotificationService(temp_db, mock_napcat)
    lvl = LevelService(mock_napcat)
    approval = ApprovalService(temp_db, lvl, audit, notif, test_config)
    access = AccessService(temp_db, approval, lvl, audit, test_config)
    return access, approval, temp_db, mock_napcat, test_config


@pytest.mark.asyncio
async def test_access_requires_active_grant(access_system):
    access, approval, db, napcat, config = access_system

    # Unapproved user
    dec = await access.check_access("bot1", "10001", "20001")
    assert dec.allowed is False
    assert dec.reason == "NO_GRANT"

    # Grant access
    await approval.grant_manually("99999", "OWNER", "bot1", "10001", "20001", 0)

    # Approved user
    dec_after = await access.check_access("bot1", "10001", "20001")
    assert dec_after.allowed is True
    assert dec_after.reason == "APPROVED"


@pytest.mark.asyncio
async def test_revocation_immediately_blocks_access(access_system):
    access, approval, db, napcat, config = access_system

    # 18. Revoked grant fails immediately
    await approval.grant_manually("99999", "OWNER", "bot1", "10001", "20001", 0)
    assert (await access.check_access("bot1", "10001", "20001")).allowed is True

    # Revoke
    await approval.revoke_grant("99999", "OWNER", "bot1", "10001", "20001", "违规发言")

    dec = await access.check_access("bot1", "10001", "20001")
    assert dec.allowed is False
    assert "REVOKED" in dec.reason


@pytest.mark.asyncio
async def test_blacklist_overrides_active_grant(access_system):
    access, approval, db, napcat, config = access_system

    # 19. Blacklist takes precedence over active grant
    await approval.grant_manually("99999", "OWNER", "bot1", "10001", "20001", 0)
    await approval.ban_user("99999", "OWNER", "bot1", "10001", "20001", "拉黑测试")

    dec = await access.check_access("bot1", "10001", "20001")
    assert dec.allowed is False
    assert "BLACKLISTED" in dec.reason


@pytest.mark.asyncio
async def test_cross_group_authorization_isolation(access_system):
    access, approval, db, napcat, config = access_system

    # 20. Distinct groups are isolated
    # Enable group 10002 as well
    pol2 = await approval.get_group_policy("bot1", "10002")
    pol2.enabled = True
    await approval.save_group_policy(pol2)

    # Grant user access ONLY in group 10001
    await approval.grant_manually("99999", "OWNER", "bot1", "10001", "20001", 0)

    # Allowed in group 10001
    assert (await access.check_access("bot1", "10001", "20001")).allowed is True

    # Denied in group 10002
    dec_g2 = await access.check_access("bot1", "10002", "20001")
    assert dec_g2.allowed is False
    assert dec_g2.reason == "NO_GRANT"


@pytest.mark.asyncio
async def test_cross_bot_id_isolation(access_system):
    access, approval, db, napcat, config = access_system

    # 40. Distinct bot_self_ids are isolated
    await approval.grant_manually("99999", "OWNER", "bot_alpha", "10001", "20001", 0)

    # Allowed under bot_alpha
    assert (await access.check_access("bot_alpha", "10001", "20001")).allowed is True

    # Denied under bot_beta
    assert (await access.check_access("bot_beta", "10001", "20001")).allowed is False


@pytest.mark.asyncio
async def test_persistence_across_restart(temp_db: Database, mock_napcat: MockNapCatClient, test_config: PluginConfig):
    # 21. Persistence across database reload / restart
    audit = AuditService(temp_db)
    notif = NotificationService(temp_db, mock_napcat)
    lvl = LevelService(mock_napcat)
    approval1 = ApprovalService(temp_db, lvl, audit, notif, test_config)

    # Grant access
    await approval1.grant_manually("99999", "OWNER", "bot1", "10001", "20001", 0)

    # Simulate restart by instantiating new service instances pointing to same SQLite database
    approval2 = ApprovalService(temp_db, lvl, audit, notif, test_config)
    access2 = AccessService(temp_db, approval2, lvl, audit, test_config)

    dec = await access2.check_access("bot1", "10001", "20001")
    assert dec.allowed is True
    assert dec.reason == "APPROVED"


@pytest.mark.asyncio
async def test_level_drop_suspends_and_recovery_restores(access_system):
    access, approval, db, napcat, config = access_system

    # 22. Level drop suspends grant
    # Setup user 20001 with LV12 initially
    napcat.set_member("10001", "20001", level="12")
    await approval.grant_manually("99999", "OWNER", "bot1", "10001", "20001", 0)

    # Set last_verified_at in the past so recheck runs
    past = int(time.time()) - 400
    await db.execute(
        "UPDATE grants SET last_verified_at = ? WHERE bot_self_id = 'bot1' AND group_id = '10001' AND user_id = '20001'",
        (past,),
    )
    await db.commit()

    # User level drops to LV7 (below minimum LV10)
    napcat.set_member("10001", "20001", level="7")

    dec_drop = await access.check_access("bot1", "10001", "20001")
    assert dec_drop.allowed is False
    assert dec_drop.reason == "LEVEL_BELOW_MINIMUM"
    assert dec_drop.grant_status == GrantStatus.SUSPENDED.value

    # 23. Level recovers to LV15: automatic restoration
    napcat.set_member("10001", "20001", level="15")
    dec_recover = await access.check_access("bot1", "10001", "20001")
    assert dec_recover.allowed is True
    assert dec_recover.reason == "APPROVED"
    assert dec_recover.grant_status == GrantStatus.ACTIVE.value
