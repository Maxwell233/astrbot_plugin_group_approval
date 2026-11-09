"""Tests for WebUI REST API endpoints."""

from __future__ import annotations

import json

import pytest

from approval.approval_service import ApprovalService
from approval.audit_service import AuditService
from approval.config import PluginConfig
from approval.database import Database
from approval.level_service import LevelService
from approval.notification_service import NotificationService
from approval.web_api import WebApiHandler
from tests.conftest import MockNapCatClient


@pytest.fixture
def web_system(temp_db: Database, mock_napcat: MockNapCatClient, test_config: PluginConfig):
    audit = AuditService(temp_db)
    notif = NotificationService(temp_db, mock_napcat)
    lvl = LevelService(mock_napcat)
    approval = ApprovalService(temp_db, lvl, audit, notif, test_config)
    web = WebApiHandler(temp_db, approval, audit, test_config)
    return web, approval, temp_db, mock_napcat, test_config


@pytest.mark.asyncio
async def test_web_api_dashboard_and_applications(web_system):
    web, approval, db, napcat, config = web_system

    # Initial stats
    dash_init = await web.get_dashboard_stats()
    assert dash_init["status"] == "ok"
    assert dash_init["data"]["stats"]["pending_applications"] == 0

    # User applies
    await approval.apply_for_access("bot1", "10001", "20001", "申请测试")

    # Stats after application
    dash_after = await web.get_dashboard_stats()
    assert dash_after["data"]["stats"]["pending_applications"] == 1

    # List applications
    apps_res = await web.list_applications(group_id="10001")
    assert apps_res["status"] == "ok"
    apps = apps_res["data"]["applications"]
    assert len(apps) == 1
    app_id = apps[0]["id"]

    # Approve via Web API
    approve_res = await web.approve_application({"application_id": app_id, "note": "Web审核通过"})
    assert approve_res["status"] == "ok"

    # List users
    users_res = await web.list_users(group_id="10001")
    assert users_res["status"] == "ok"
    assert len(users_res["data"]["grants"]) == 1


@pytest.mark.asyncio
async def test_web_api_group_policy_update(web_system):
    web, approval, db, napcat, config = web_system

    # Update group policy via API
    update_res = await web.update_group_policy({
        "group_id": "10001",
        "min_level": 20,
        "enabled": True,
        "grant_duration_days": 30,
    })
    assert update_res["status"] == "ok"

    # Check updated policy
    pol = await approval.get_group_policy("default", "10001")
    assert pol.min_level == 20
    assert pol.grant_duration_seconds == 30 * 86400


@pytest.mark.asyncio
async def test_web_api_audit_export(web_system):
    web, approval, db, napcat, config = web_system

    # Produce some audit actions
    await approval.grant_manually("WebAdmin", "REVIEWER", "bot1", "10001", "20001", 0)

    # Export JSON
    json_str = await web.export_audit_logs("json")
    logs = json.loads(json_str)
    assert len(logs) >= 1
    assert logs[0]["action"] == "MANUAL_GRANT"

    # Export CSV
    csv_str = await web.export_audit_logs("csv")
    assert "timestamp,actor_id" in csv_str
    assert "MANUAL_GRANT" in csv_str
