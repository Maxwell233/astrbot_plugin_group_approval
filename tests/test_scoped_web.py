"""Production Dashboard route handlers and the scoped authorization boundary."""

import pytest

import main


class Request:
    def __init__(self, username, payload):
        self.username = username
        self.payload = payload
        self.query = {"bot_self_id": payload.get("bot_self_id", "")} if isinstance(payload, dict) else {}

    async def json(self, default=None):
        return self.payload


async def test_dashboard_checks_owner_and_current_bot_before_mutating(dashboard_plugin, monkeypatch):
    payload = {
        "bot_self_id": "11111", "action": "grant", "user_id": "30001", "scope": "GROUP",
        "level_exempt": True, "membership_exempt": True,
    }
    monkeypatch.setattr(main, "request", Request("api_key:1", payload))
    assert (await dashboard_plugin._web_action())["retcode"] == 403
    monkeypatch.setattr(main, "request", Request("reviewer", payload))
    assert (await dashboard_plugin._web_action())["retcode"] == 403
    monkeypatch.setattr(main, "request", Request("dashboard-admin", {**payload, "bot_self_id": "99999"}))
    assert (await dashboard_plugin._web_action())["retcode"] == 403
    assert not await dashboard_plugin.db.fetchone("SELECT 1 FROM grants")

    monkeypatch.setattr(main, "request", Request("dashboard-admin", payload))
    assert (await dashboard_plugin._web_action())["status"] == "ok"
    assert (await dashboard_plugin._web_state())["data"]["grants"][0]["bot_self_id"] == "11111"
    monkeypatch.setattr(main, "request", Request("dashboard-admin", {**payload, "bot_self_id": "22222"}))
    assert not (await dashboard_plugin._web_state())["data"]["grants"]


async def test_dashboard_rejects_a_non_object_request_body(dashboard_plugin, monkeypatch):
    monkeypatch.setattr(main, "request", Request("dashboard-admin", []))
    assert (await dashboard_plugin._web_action())["retcode"] == 400
    assert not await dashboard_plugin.db.fetchone("SELECT 1 FROM audit_log")


@pytest.mark.parametrize("action", ["approve", "reject"])
async def test_dashboard_cannot_use_another_bots_application_id(dashboard_plugin, monkeypatch, action):
    await dashboard_plugin.db.execute(
        """INSERT INTO applications (id, bot_self_id, user_id, scope, primary_group_id,
           primary_revision, level, status, created_at) VALUES
           ('L123456789abc', '11111', '30001', 'GROUP', '10001', 1, 20, 'PENDING', 1)"""
    )
    payload = {"bot_self_id": "22222", "action": action, "application_id": "L123456789abc"}
    monkeypatch.setattr(main, "request", Request("dashboard-admin", payload))
    assert (await dashboard_plugin._web_action())["status"] == "error"
    assert (await dashboard_plugin.scopes.application("11111", "L123456789abc"))["status"] == "PENDING"
