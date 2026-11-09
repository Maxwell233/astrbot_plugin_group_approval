"""Production Dashboard route handlers and the scoped authorization boundary."""

from types import SimpleNamespace

import pytest

import main
from approval.auth_service import AuthorizationService


class Request:
    def __init__(self, username, payload):
        self.username = username
        self.payload = payload
        self.query = {"bot_self_id": payload.get("bot_self_id", "")}

    async def json(self, default=None):
        return self.payload


@pytest.fixture
async def plugin(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "StarTools", SimpleNamespace(get_data_dir=lambda: tmp_path))
    monkeypatch.setattr(main, "ASTRBOT_AVAILABLE", True)
    instance = main.GroupApprovalPlugin(
        config={"enabled": False, "notifications": {"retry_enabled": False}}
    )
    instance.auth = AuthorizationService(instance.plugin_config, lambda: "dashboard-admin")

    async def verify(bot):
        return bot in {"11111", "22222"}

    async def list_bots():
        return ["11111", "22222"]

    monkeypatch.setattr(instance.napcat, "verify_bot", verify)
    monkeypatch.setattr(instance.napcat, "list_bots", list_bots)
    await instance.initialize()
    try:
        yield instance
    finally:
        await instance.terminate()


async def test_dashboard_checks_owner_and_current_bot_before_mutating(plugin, monkeypatch):
    payload = {
        "bot_self_id": "11111", "action": "grant", "user_id": "30001", "scope": "GROUP",
        "level_exempt": True, "membership_exempt": True,
    }
    monkeypatch.setattr(main, "request", Request("api_key:1", payload))
    assert (await plugin._web_action())["retcode"] == 403
    monkeypatch.setattr(main, "request", Request("reviewer", payload))
    assert (await plugin._web_action())["retcode"] == 403
    monkeypatch.setattr(main, "request", Request("dashboard-admin", {**payload, "bot_self_id": "99999"}))
    assert (await plugin._web_action())["retcode"] == 403
    assert not await plugin.db.fetchone("SELECT 1 FROM grants_v2")

    monkeypatch.setattr(main, "request", Request("dashboard-admin", payload))
    assert (await plugin._web_action())["status"] == "ok"
    assert (await plugin._web_state())["data"]["grants"][0]["bot_self_id"] == "11111"
    monkeypatch.setattr(main, "request", Request("dashboard-admin", {**payload, "bot_self_id": "22222"}))
    assert not (await plugin._web_state())["data"]["grants"]


async def test_dashboard_cannot_use_another_bots_application_id(plugin, monkeypatch):
    await plugin.db.execute(
        """INSERT INTO applications_v2 (id, bot_self_id, user_id, scope, primary_group_id,
           primary_revision, level, status, created_at) VALUES
           ('L123456789abc', '11111', '30001', 'GROUP', '10001', 1, 20, 'PENDING', 1)"""
    )
    payload = {"bot_self_id": "22222", "action": "reject", "application_id": "L123456789abc"}
    monkeypatch.setattr(main, "request", Request("dashboard-admin", payload))
    assert (await plugin._web_action())["status"] == "error"
    assert (await plugin.scopes.application("11111", "L123456789abc"))["status"] == "PENDING"
