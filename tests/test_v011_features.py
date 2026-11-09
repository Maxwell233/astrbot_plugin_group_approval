"""Comprehensive tests for v0.1.1 security fixes, transaction safety, and new capabilities."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from approval.approval_service import ApprovalService
from approval.config import PluginConfig
from approval.database import Database
from approval.models import GrantStatus
from approval.napcat_client import NapCatClient, normalize_onebot_response
from approval.web_api import WebApiHandler
from main import (
    GroupApprovalPlugin,
    _get_query_param,
    _get_request_json,
    _get_request_operator,
)
from tests.conftest import MockNapCatClient


# 1. NapCat Normalization & Multi-bot tests
def test_napcat_normalization_formats():
    # Wrapped OneBot v11 format
    wrapped = {"status": "ok", "retcode": 0, "data": {"level": 15, "user_id": 123}}
    ok, data, err, code = normalize_onebot_response(wrapped)
    assert ok is True
    assert data["level"] == 15
    assert code == 0

    # Unpacked / direct dictionary format
    direct = {"message_id": 9999, "status": "sent"}
    ok2, data2, err2, code2 = normalize_onebot_response(direct)
    assert ok2 is True
    assert data2["message_id"] == 9999

    # OneBot error format
    err_resp = {"status": "failed", "retcode": 10001, "msg": "Member not in group"}
    ok3, data3, err3, code3 = normalize_onebot_response(err_resp)
    assert ok3 is False
    assert code3 == 10001
    assert "Member not in group" in err3

    # Empty or None response
    ok4, _, _, code4 = normalize_onebot_response(None)
    assert ok4 is False
    assert code4 == -4


@pytest.mark.asyncio
async def test_napcat_multi_bot_resolution():
    # Simulate AstrBot platform manager with two bot instances
    mock_context = MagicMock()
    mock_context.get_platform = None
    pm = MagicMock()
    inst1 = MagicMock()
    inst1.adapter_type = "aiocqhttp"
    inst1.self_id = "11111"
    inst1.get_client.return_value = inst1
    inst1.bot = inst1

    inst2 = MagicMock()
    inst2.adapter_type = "aiocqhttp"
    inst2.self_id = "22222"
    inst2.get_client.return_value = inst2
    inst2.bot = inst2

    pm.get_insts.return_value = [inst1, inst2]
    mock_context.platform_manager = pm

    client = NapCatClient()
    client.set_context(mock_context)

    # Resolve specific bot
    c1 = client._resolve_bot_client("11111")
    assert c1.self_id == "11111"

    c2 = client._resolve_bot_client("22222")
    assert c2.self_id == "22222"


# 2. Database Transactions and Rollback
@pytest.mark.asyncio
async def test_transaction_rollback_on_error(temp_db: Database, mock_napcat: MockNapCatClient, test_config: PluginConfig):
    # Verify transaction rollback reverts database changes
    async with temp_db.transaction() as tx:
        await temp_db.execute(
            "INSERT INTO reviewers (qq_id, added_by, added_at) VALUES ('77777', 'tester', 100)"
        )
        # Verify inserted in same transaction
        r = await temp_db.fetchone("SELECT qq_id FROM reviewers WHERE qq_id = '77777'")
        assert r is not None

    # Rollback simulation
    try:
        async with temp_db.transaction():
            await temp_db.execute(
                "INSERT INTO reviewers (qq_id, added_by, added_at) VALUES ('66666', 'tester', 200)"
            )
            raise RuntimeError("Simulated failure during write transaction")
    except RuntimeError:
        pass

    # Verify 66666 was rolled back
    r_rolled = await temp_db.fetchone("SELECT qq_id FROM reviewers WHERE qq_id = '66666'")
    assert r_rolled is None
    # Verify earlier committed record persists
    r_persisted = await temp_db.fetchone("SELECT qq_id FROM reviewers WHERE qq_id = '77777'")
    assert r_persisted is not None


# 3. Level Exemption and Manual Grants
@pytest.mark.asyncio
async def test_level_exemption_lifecycle(access_system):
    access, approval, db, napcat, config = access_system

    # Non-owner cannot grant level exemption
    ok_non_owner, msg_non_owner = await approval.grant_manually(
        actor_id="88888",
        actor_role="REVIEWER",
        bot_self_id="bot1",
        group_id="10001",
        user_id="20005",
        is_level_exempt=True,
    )
    assert ok_non_owner is False
    assert "仅机器人主人" in msg_non_owner

    # Owner grants level exemption
    ok_owner, msg_owner = await approval.grant_manually(
        actor_id="99999",
        actor_role="OWNER",
        bot_self_id="bot1",
        group_id="10001",
        user_id="20005",
        is_level_exempt=True,
    )
    assert ok_owner is True
    grant = await approval.get_active_grant("bot1", "10001", "20005")
    assert grant.is_level_exempt is True
    assert grant.grant_type == "LEVEL_EXEMPTION"

    # Set last_verified_at in the past
    past = int(time.time()) - 400
    await db.execute(
        "UPDATE grants SET last_verified_at = ? WHERE bot_self_id = 'bot1' AND group_id = '10001' AND user_id = '20005'",
        (past,),
    )
    await db.commit()

    # User level is below threshold (LV5 < LV10)
    napcat.set_member("10001", "20005", level="5")

    # Access is ALLOWED because grant is level-exempt!
    dec = await access.check_access("bot1", "10001", "20005")
    assert dec.allowed is True
    assert dec.reason == "APPROVED"


# 4. Rate Limiting Tests
@pytest.mark.asyncio
async def test_application_rate_limiting(access_system):
    access, approval, db, napcat, config = access_system
    config.application.rate_limit_per_hour = 2
    napcat.set_member("10001", "20010", level="15")

    # 1st application: OK
    ok1, _, app1 = await approval.apply_for_access("bot1", "10001", "20010")
    assert ok1 is True
    # Cancel so user can apply again
    await approval.cancel_application("20010", "bot1", "10001")

    # 2nd application: OK
    ok2, _, app2 = await approval.apply_for_access("bot1", "10001", "20010")
    assert ok2 is True
    await approval.cancel_application("20010", "bot1", "10001")

    # 3rd application: Exceeded rate limit (limit is 2/hr)
    ok3, msg3, app3 = await approval.apply_for_access("bot1", "10001", "20010")
    assert ok3 is False
    assert "过于频繁" in msg3


# 5. Reviewer Persistence and Group vs Global Unban
@pytest.mark.asyncio
async def test_reviewer_persistence_in_db(access_system):
    access, approval, db, napcat, config = access_system

    # Add reviewer
    ok, msg = await approval.add_reviewer("33333", added_by="99999")
    assert ok is True
    assert await approval.is_reviewer("33333") is True
    assert "33333" in (await approval.list_reviewers())

    # Remove reviewer
    ok_rm, msg_rm = await approval.remove_reviewer("33333")
    assert ok_rm is True
    assert await approval.is_reviewer("33333") is False


@pytest.mark.asyncio
async def test_group_unban_does_not_clear_global_ban(access_system):
    access, approval, db, napcat, config = access_system

    # Globally ban user 20020
    await approval.ban_user("99999", "OWNER", "bot1", "ALL", "20020", "Global violation")

    # Verify blacklisted in group 10001
    banned1, _ = await approval.is_blacklisted("bot1", "10001", "20020")
    assert banned1 is True

    # Try to unban specifically from group 10001
    ok_unban_group, msg = await approval.unban_user("99999", "OWNER", "bot1", "10001", "20020")
    assert ok_unban_group is False  # User was not banned in 10001 specifically

    # Global ban still holds!
    banned_still, _ = await approval.is_blacklisted("bot1", "10001", "20020")
    assert banned_still is True

    # Unban globally
    ok_unban_global, msg_global = await approval.unban_user("99999", "OWNER", "bot1", "ALL", "20020")
    assert ok_unban_global is True

    # Now no longer banned
    banned_cleared, _ = await approval.is_blacklisted("bot1", "10001", "20020")
    assert banned_cleared is False


# 6. Web API Modern Request Parsing and Bots Endpoint
@pytest.mark.asyncio
async def test_web_api_v011_features(web_system):
    web, approval, db, napcat, config = web_system

    # Test /bots endpoint
    bots_res = await web.list_bots()
    assert bots_res["status"] == "ok"
    assert "bots" in bots_res["data"]
    assert len(bots_res["data"]["bots"]) >= 1

    # Test modern query parameter helper
    class MockRequest:
        def __init__(self, query=None, args=None, headers=None, json_body=None):
            self.query = query or {}
            self.args = args or {}
            self.headers = headers or {}
            self._json = json_body or {}

        async def json(self, default=None):
            return self._json

    req = MockRequest(
        query={"group_id": "10001"},
        headers={"X-AstrBot-User": "admin_alice"},
        json_body={"application_id": "A1000", "note": "Approve via test"},
    )

    gid = _get_query_param(req, "group_id")
    assert gid == "10001"

    operator = _get_request_operator(req)
    assert operator == "admin_alice"

    body = await _get_request_json(req)
    assert body["application_id"] == "A1000"


# 7. GroupGuardian Dynamic Promotion and Stop Event
@pytest.mark.asyncio
async def test_group_guardian_priority_promotion():
    mock_context = MagicMock()
    gg_handler = MagicMock()
    gg_handler.name = "group_guardian_filter"
    gg_handler.priority = 0

    other_handler = MagicMock()
    other_handler.name = "jrys_handler"
    other_handler.priority = 0

    mock_context.handlers = [gg_handler, other_handler]

    plugin = GroupApprovalPlugin(context=mock_context)
    plugin._promote_group_guardian_priority()

    # GroupGuardian handler must be promoted to priority 100!
    assert gg_handler.priority == 100
    # Other plugin handler unchanged
    assert other_handler.priority == 0
    await plugin.terminate()
