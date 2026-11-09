"""Integration tests for gate decisions and access enforcement.

Scope note (strict test taxonomy):
* These are **service-level integration tests**: real services, real SQLite, a fake NapCat
  client (no QQ/NapCat available here) and a lightweight event object.
* They do NOT prove AstrBot framework dispatch order. That is covered by
  ``tests/test_framework_handler_order.py``, which runs against a real AstrBot install.
* They do NOT prove LivingMemory isolation; see ``INTEGRATION_TEST_REPORT.md`` for the
  verified framework limitation.
"""

from __future__ import annotations

import pytest

from approval.access_service import AccessService
from approval.approval_service import ApprovalService
from approval.audit_service import AuditService
from approval.config import PluginConfig
from approval.database import Database
from approval.gate import AdmissionGate, AdmissionKind
from approval.level_service import LevelService
from approval.notification_service import NotificationService
from tests.conftest import MockNapCatClient


class DummyEvent:
    """Minimal event object recording interception and replies."""

    def __init__(self, bot_self_id: str, group_id: str | None, user_id: str, message_str: str):
        self.bot_self_id = bot_self_id
        self.group_id = group_id
        self.user_id = user_id
        self.message_str = message_str
        self.stopped = False
        self.replies: list[str] = []

    def get_self_id(self) -> str:
        return self.bot_self_id

    def get_group_id(self) -> str | None:
        return self.group_id

    def get_sender_id(self) -> str:
        return self.user_id

    def stop_event(self) -> None:
        self.stopped = True

    def plain_result(self, text: str) -> str:
        self.replies.append(text)
        return text


@pytest.fixture
def gate_system(temp_db: Database, mock_napcat: MockNapCatClient, test_config: PluginConfig):
    audit = AuditService(temp_db)
    notif = NotificationService(temp_db, mock_napcat)
    lvl = LevelService(mock_napcat)
    approval = ApprovalService(temp_db, lvl, audit, notif, test_config)
    access = AccessService(temp_db, approval, lvl, audit, test_config)
    gate = AdmissionGate(access, test_config)
    return gate, access, approval, temp_db, mock_napcat, test_config


async def decide(gate: AdmissionGate, bot: str, group: str | None, user: str, text: str, **kw):
    return await gate.decide(
        bot_self_id=bot,
        group_id=group,
        user_id=user,
        message_str=text,
        **kw,
    )


@pytest.mark.asyncio
async def test_unauthorized_user_blocked_from_ai_and_plugins(gate_system):
    gate, access, approval, db, napcat, config = gate_system
    unauth_user = "20001"  # member with no grant

    # Ordinary AI request addressed to the bot
    d1 = await decide(gate, "bot1", "10001", unauth_user, "请帮我写一段代码", is_at_bot=True)
    assert d1.kind is AdmissionKind.BLOCK
    assert d1.notice is not None and "权限拦截" in d1.notice

    # Third-party plugin commands, prefixed and keyword forms
    for text in ("/jrys", "/塔罗", "/pig", "今日小猪", "今日运势"):
        d = await decide(gate, "bot1", "10001", unauth_user, text)
        assert d.kind is AdmissionKind.BLOCK, text

    # Empty @Bot invocation must also be blocked
    d_at = await decide(gate, "bot1", "10001", unauth_user, "", is_at_bot=True)
    assert d_at.kind is AdmissionKind.BLOCK
    assert d_at.notice is not None

    # Image-only / untargeted chat: blocked silently (no group spam)
    d_img = await decide(gate, "bot1", "10001", unauth_user, "[图片]")
    assert d_img.kind is AdmissionKind.BLOCK
    assert d_img.notice is None


@pytest.mark.asyncio
async def test_admission_commands_are_classified_for_local_dispatch(gate_system):
    """Admission commands must be classified (never "allow through") so the gate executes them."""
    gate, _, _, _, _, _ = gate_system
    unauthorized = "20001"

    for command in ("申请使用", "我的权限", "申请状态", "取消申请", "使用规则"):
        d = await decide(gate, "bot1", "10001", unauthorized, f"{command} 理由", is_command=True)
        assert d.kind is AdmissionKind.USER_COMMAND, command
        assert d.command == command

    # Admin command is classified for authorization inside the dispatcher.
    d_ga = await decide(gate, "bot1", "10001", unauthorized, "ga approve A1000", is_command=True)
    assert d_ga.kind is AdmissionKind.ADMIN_COMMAND
    assert d_ga.args == ["approve", "A1000"]

    # Fuzzy / prefix strings must NOT be treated as the admin command even with is_command.
    for text in ("garbage", "game start", "ga", "gapprove"):
        d = await decide(gate, "bot1", "10001", unauthorized, text, is_command=True)
        if text == "ga":
            # exact token == the admin command, no args
            assert d.kind is AdmissionKind.ADMIN_COMMAND
            assert d.args == []
        else:
            assert d.kind is AdmissionKind.BLOCK, text

    # Without AstrBot's command wake flag, a bare token is ordinary text, not a command.
    d_plain = await decide(gate, "bot1", "10001", unauthorized, "申请使用", is_command=False)
    assert d_plain.kind is AdmissionKind.BLOCK


@pytest.mark.asyncio
async def test_authorized_user_is_allowed(gate_system):
    gate, access, approval, db, napcat, config = gate_system
    napcat.set_member("10001", "20001", level="15")
    await approval.grant_manually("99999", "OWNER", "bot1", "10001", "20001", 0)

    d = await decide(gate, "bot1", "10001", "20001", "请帮我写一段代码", is_at_bot=True)
    assert d.kind is AdmissionKind.ALLOW


@pytest.mark.asyncio
async def test_missing_bot_identity_fails_closed(gate_system):
    """No placeholder bot id: an unknown identity must never be evaluated as authorized."""
    gate, _, _, _, _, _ = gate_system
    d = await decide(gate, "", "10001", "20001", "hello")
    assert d.kind is AdmissionKind.BLOCK
    assert d.notice is not None and "无法确认" in d.notice


@pytest.mark.asyncio
async def test_private_chat_and_admin_commands(gate_system):
    gate, access, approval, db, napcat, config = gate_system

    # Normal user private chat intercepted (private_chat disabled for normal users)
    d_user = await decide(gate, "bot1", None, "20001", "你好")
    assert d_user.kind is AdmissionKind.BLOCK
    assert d_user.notice is not None and "未开启私聊普通对话" in d_user.notice

    # Owner's /ga in private is classified for local dispatch (authorization inside dispatcher)
    d_admin = await decide(gate, "bot1", None, "99999", "ga status", is_command=True)
    assert d_admin.kind is AdmissionKind.ADMIN_COMMAND


@pytest.mark.asyncio
async def test_fail_closed_on_napcat_disconnect(gate_system):
    gate, access, approval, db, napcat, config = gate_system
    napcat.set_member("10001", "20001", level="15")
    await approval.grant_manually("99999", "OWNER", "bot1", "10001", "20001", 0)

    await db.execute(
        "UPDATE grants SET last_verified_at = ? WHERE bot_self_id = 'bot1' AND group_id = '10001' AND user_id = '20001'",
        (100,),
    )
    await db.commit()

    napcat.network_error_on_next = True
    dec = await access.check_access("bot1", "10001", "20001")
    assert dec.allowed is False
    assert "LEVEL_CHECK_FAILED" in dec.reason
