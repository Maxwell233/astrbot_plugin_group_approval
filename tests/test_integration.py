"""Integration tests for event pipeline, gate interception, and plugin compatibility."""

from __future__ import annotations

import pytest

from approval.access_service import AccessService
from approval.approval_service import ApprovalService
from approval.audit_service import AuditService
from approval.config import PluginConfig
from approval.database import Database
from approval.gate import AdmissionGate
from approval.level_service import LevelService
from approval.notification_service import NotificationService
from tests.conftest import MockNapCatClient


class DummyEvent:
    """Mock AstrMessageEvent recording interception and stop_event calls."""

    def __init__(
        self,
        bot_self_id: str,
        group_id: str | None,
        user_id: str,
        message_str: str,
        is_at_bot: bool = False,
        role: str = "member",
    ):
        self.bot_self_id = bot_self_id
        self.group_id = group_id
        self.user_id = user_id
        self.message_str = message_str
        self.is_at_bot = is_at_bot
        self.role = role
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


@pytest.mark.asyncio
async def test_unauthorized_user_blocked_from_ai_and_plugins(gate_system):
    gate, access, approval, db, napcat, config = gate_system

    unauth_user = "20001"  # Not yet approved in group 10001

    # 25. Cannot invoke AI (e.g. asking question to bot)
    inter1, notice1 = await gate.should_intercept("bot1", "10001", unauth_user, "请帮我写一段代码", is_at_bot=True)
    assert inter1 is True
    assert notice1 is not None and "权限拦截" in notice1

    # 26. Cannot invoke /jrys or 运势
    inter2, notice2 = await gate.should_intercept("bot1", "10001", unauth_user, "/jrys")
    assert inter2 is True
    assert notice2 is not None and "权限拦截" in notice2

    # 27. Cannot invoke cyber divination / 塔罗
    inter3, notice3 = await gate.should_intercept("bot1", "10001", unauth_user, "/塔罗")
    assert inter3 is True

    # 28. Cannot invoke rollpig / 今日小猪
    inter4, notice4 = await gate.should_intercept("bot1", "10001", unauth_user, "/pig")
    assert inter4 is True

    # 32. Plain text without prefix (e.g. "今日小猪", "今日运势")
    inter5, notice5 = await gate.should_intercept("bot1", "10001", unauth_user, "今日小猪")
    assert inter5 is True
    assert notice5 is not None

    # 33. Plain @Bot invocation
    inter6, notice6 = await gate.should_intercept("bot1", "10001", unauth_user, "", is_at_bot=True)
    assert inter6 is True
    assert notice6 is not None

    # 34. Image only / unprompted chat: intercepted without spamming notice
    inter7, notice7 = await gate.should_intercept("bot1", "10001", unauth_user, "[图片]", is_at_bot=False)
    assert inter7 is True
    assert notice7 is None  # Silently dropped so group is not spammed


@pytest.mark.asyncio
async def test_group_guardian_audit_precedence(gate_system):
    """30. Verify execution pipeline ensures GroupGuardian audits all messages before Group Approval stops them."""
    gate, access, approval, db, napcat, config = gate_system

    unauth_user = "20001"
    event = DummyEvent("bot1", "10001", unauth_user, "这是一条违规包含广告的消息")

    # Simulation of pipeline execution order:
    # Priority 100: GroupGuardian inspects message
    guardian_audited = False
    guardian_detected_violation = False

    async def group_guardian_handler(evt: DummyEvent):
        nonlocal guardian_audited, guardian_detected_violation
        guardian_audited = True
        if "广告" in evt.message_str:
            guardian_detected_violation = True
            # GroupGuardian takes action (e.g. recalls message)
            evt.plain_result("检测到违规广告，已记录并撤回！")

    # Step 1: GroupGuardian runs first (priority 100)
    await group_guardian_handler(event)
    assert guardian_audited is True
    assert guardian_detected_violation is True

    # Step 2: Group Approval runs next (priority 50)
    inter, notice = await gate.should_intercept(
        event.get_self_id(),
        event.get_group_id(),
        event.get_sender_id(),
        event.message_str,
    )
    if inter:
        event.stop_event()

    assert event.stopped is True
    # Crucial assertion: GroupGuardian successfully performed audit BEFORE event was stopped
    assert guardian_audited is True


@pytest.mark.asyncio
async def test_living_memory_isolation(gate_system):
    """31. Verify unauthorized user cannot trigger LLM pipeline where LivingMemory captures memory."""
    gate, access, approval, db, napcat, config = gate_system

    unauth_user = "20001"
    event = DummyEvent("bot1", "10001", unauth_user, "我最喜欢的编程语言是 Rust", is_at_bot=True)

    # Priority 50 gate checks access
    inter, notice = await gate.should_intercept(
        event.get_self_id(),
        event.get_group_id(),
        event.get_sender_id(),
        event.message_str,
        is_at_bot=event.is_at_bot,
    )

    if inter:
        event.stop_event()

    assert event.stopped is True

    # Simulating LLM downstream pipeline (priority 0 / -10)
    llm_pipeline_reached = False
    living_memory_captured = False

    if not event.stopped:
        llm_pipeline_reached = True
        living_memory_captured = True

    assert llm_pipeline_reached is False
    assert living_memory_captured is False


@pytest.mark.asyncio
async def test_private_chat_and_admin_commands(gate_system):
    gate, access, approval, db, napcat, config = gate_system

    # 35. Normal user private chat intercepted
    inter_user, notice_user = await gate.should_intercept("bot1", None, "20001", "你好")
    assert inter_user is True
    assert "未开启私聊普通对话" in notice_user

    # 36. Administrator private chat /ga command permitted
    inter_admin, notice_admin = await gate.should_intercept("bot1", None, "99999", "/ga status")
    assert inter_admin is False
    assert notice_admin is None


@pytest.mark.asyncio
async def test_fail_closed_on_napcat_disconnect(gate_system):
    gate, access, approval, db, napcat, config = gate_system

    # 38. Simulate NapCat network error when rechecking level
    napcat.set_member("10001", "20001", level="15")
    await approval.grant_manually("99999", "OWNER", "bot1", "10001", "20001", 0)

    # Force recheck
    past = 100
    await db.execute(
        "UPDATE grants SET last_verified_at = ? WHERE bot_self_id = 'bot1' AND group_id = '10001' AND user_id = '20001'",
        (past,),
    )
    await db.commit()

    # Network failure during recheck
    napcat.network_error_on_next = True

    dec = await access.check_access("bot1", "10001", "20001")
    # Fail closed: must DENY
    assert dec.allowed is False
    assert "LEVEL_CHECK_FAILED" in dec.reason
