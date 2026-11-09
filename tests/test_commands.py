"""Tests for user commands and administrator control commands."""

from __future__ import annotations

import pytest

from approval.approval_service import ApprovalService
from approval.audit_service import AuditService
from approval.commands import CommandDispatcher
from approval.config import PluginConfig
from approval.database import Database
from approval.level_service import LevelService
from approval.notification_service import NotificationService
from tests.conftest import MockNapCatClient


@pytest.fixture
def cmd_system(temp_db: Database, mock_napcat: MockNapCatClient, test_config: PluginConfig):
    audit = AuditService(temp_db)
    notif = NotificationService(temp_db, mock_napcat)
    lvl = LevelService(mock_napcat)
    approval = ApprovalService(temp_db, lvl, audit, notif, test_config)
    dispatcher = CommandDispatcher(temp_db, approval, audit, test_config)
    return dispatcher, approval, temp_db, mock_napcat, test_config


@pytest.mark.asyncio
async def test_user_commands_flow(cmd_system):
    dispatcher, approval, db, napcat, config = cmd_system

    # 1. /使用规则
    rules_resp = await dispatcher.handle_rules("bot1", "10001")
    assert "准入规则" in rules_resp
    assert "LV10" in rules_resp

    # 2. /我的权限 (before apply)
    my_status_before = await dispatcher.handle_my_status("bot1", "10001", "20001")
    assert "未授权" in my_status_before

    # 3. /申请使用
    apply_resp = await dispatcher.handle_apply("bot1", "10001", "20001", "申请体验")
    assert "申请已提交" in apply_resp

    # 4. /申请状态
    app_status = await dispatcher.handle_application_status("bot1", "10001", "20001")
    assert "待审核申请信息" in app_status

    # 5. /取消申请
    cancel_resp = await dispatcher.handle_cancel_application("bot1", "10001", "20001")
    assert "成功取消" in cancel_resp


@pytest.mark.asyncio
async def test_admin_commands_permissions(cmd_system):
    dispatcher, approval, db, napcat, config = cmd_system

    # 14. Unauthorized ordinary user cannot execute /ga
    res_unauth = await dispatcher.handle_admin_command("bot1", "20001", "10001", ["pending"])
    assert "权限不足" in res_unauth

    # Reviewer (88888) can execute /ga pending
    res_pending = await dispatcher.handle_admin_command("bot1", "88888", "10001", ["pending"])
    assert "暂无待审核申请" in res_pending or "待审核申请列表" in res_pending

    # 24. Reviewer CANNOT execute Owner-only command /ga grant or /ga ban
    res_rev_grant = await dispatcher.handle_admin_command("bot1", "88888", "10001", ["grant", "10001", "20001"])
    assert "仅限插件 Owner 执行" in res_rev_grant

    # Owner (99999) can execute /ga grant
    res_owner_grant = await dispatcher.handle_admin_command("bot1", "99999", "10001", ["grant", "10001", "20001"])
    assert "已手动向用户" in res_owner_grant

    # Check status command
    res_status = await dispatcher.handle_admin_command("bot1", "99999", "10001", ["status"])
    assert "运行状态" in res_status
    assert "受管启用群数" in res_status
