"""Comprehensive tests for v0.1.1 security fixes, transaction safety, and new capabilities."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from approval.approval_service import ApprovalService
from approval.auth_service import AuthorizationService
from approval.config import PluginConfig
from approval.database import Database
from approval.models import GrantStatus
from approval.napcat_client import NapCatClient, normalize_onebot_response
from approval.web_api import WebApiHandler
from main import (
    GroupApprovalPlugin,
    _get_query_param,
    _get_request_json,
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
async def test_napcat_identity_requires_verified_mapping():
    """No context / no verified get_login_info => fail closed (no first-client fallback).

    The old mock-based test asserted a synchronous `_resolve_bot_client` selection based on
    a nonexistent ``.self_id`` attribute; that was exactly the broken design. Real verified
    multi-bot routing is covered by tests/test_bot_identity.py.
    """
    from approval.napcat_client import ERR_UNKNOWN_BOT_SELF_ID

    client = NapCatClient()
    assert await client.list_bots() == []

    ok, data, err, code = await client.send_private_msg("123", "hi", bot_self_id="99999")
    assert ok is False
    assert data is None
    assert code == -3, (err, code)
    assert err.startswith(ERR_UNKNOWN_BOT_SELF_ID), err


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

    # Owner grants level exemption to a verified member below the threshold.
    napcat.set_member("10001", "20005", level="5")
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


# 6. Web API Modern Request Parsing, Bot Routing, and Auth Verification
@pytest.mark.asyncio
async def test_web_api_v011_features(web_system):
    web, approval, db, napcat, config = web_system

    # Live discovery stays empty while historical policies remain available for reads.
    napcat.verified_bot_ids.clear()
    bots_res = await web.list_bots()
    assert bots_res["status"] == "ok"
    assert bots_res["data"]["verified_bots"] == []

    await db.execute(
        "INSERT INTO group_policies (bot_self_id, group_id, enabled, min_level, created_at, updated_at)"
        " VALUES ('10001', '10001', 1, 10, 100, 100)"
    )
    assert (await web.list_bots())["data"]["verified_bots"] == []
    assert (await web.list_groups("10001"))["data"]["groups"][0]["bot_self_id"] == "10001"

    # Test modern query parameter helper
    class MockRequest:
        def __init__(self, query=None, args=None, headers=None, username=None, json_body=None):
            self.query = query or {}
            self.args = args or {}
            self.headers = headers or {}
            self.username = username
            self._json = json_body or {}

        async def json(self, default=None):
            return self._json

    # Dashboard authorization uses ONLY AstrBot's authenticated request.username.
    auth = AuthorizationService(config, dashboard_username_getter=lambda: "astrbot")

    # Spoofed header without an authenticated session must be rejected.
    req_spoofed = MockRequest(
        query={"group_id": "10001"},
        headers={"X-AstrBot-User": "attacker_spoof"},
        json_body={"application_id": "A1000", "note": "Approve via test"},
    )
    assert _get_query_param(req_spoofed, "group_id") == "10001"
    assert auth.get_authenticated_user(req_spoofed) is None

    # Authenticated dashboard operator is accepted.
    req_auth = MockRequest(
        query={"group_id": "10001"},
        username="astrbot",
        json_body={"application_id": "A1000", "note": "Approve via test"},
    )
    assert auth.get_authenticated_user(req_auth) == "astrbot"
    body = await _get_request_json(req_auth)
    assert body["application_id"] == "A1000"

    # API-key machine identities are not verified human dashboard operators.
    assert auth.get_authenticated_user(MockRequest(username="api_key:abc123")) is None

    # Dashboard role: only the configured dashboard username is OWNER. The old hardcoded
    # 'admin'/'root'/'owner' name mapping is gone, and QQ owner IDs are a separate identity
    # system that must NOT grant dashboard privileges.
    assert await auth.get_user_role("astrbot") == "OWNER"
    for name in ("admin", "root", "owner", "99999", "88888"):
        assert await auth.get_user_role(name) == "DASHBOARD_USER", name

    # Unknown dashboard identity (getter unavailable) must fail closed for mutations.
    auth_unknown = AuthorizationService(config, dashboard_username_getter=lambda: None)
    ok_unknown, role_unknown, _ = await auth_unknown.authorize_action("astrbot", "approve")
    assert ok_unknown is False
    assert role_unknown == "DASHBOARD_USER"


# Moderation registry coverage uses AstrBot's actual metadata and decorators.
def test_no_runtime_moderation_priority_mutation():
    plugin = GroupApprovalPlugin(context=MagicMock())
    assert not hasattr(plugin, "_promote_group_guardian_priority")
    assert not hasattr(plugin, "on_message_gate_priority_mutation")
