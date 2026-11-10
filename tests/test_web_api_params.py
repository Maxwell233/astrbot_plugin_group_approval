"""Strict request validation at the current Dashboard's mutation boundary."""

import pytest

import main
from tests.test_scoped_web import Request

BOT = "11111"
BASE = {"bot_self_id": BOT, "user_id": "30001", "scope": "GROUP"}
ACTIONS = (
    "configure", "grant", "exemptions", "ban", "unban", "revoke", "restore",
    "approve", "reject", "reviewer_add", "reviewer_remove",
)


async def action(dashboard_plugin, monkeypatch, **payload):
    monkeypatch.setattr(main, "request", Request("dashboard-admin", payload))
    return await dashboard_plugin._web_action()


@pytest.mark.parametrize("name", ACTIONS)
async def test_every_write_requires_a_verified_bot(dashboard_plugin, monkeypatch, name):
    for bot in (None, "", " ", "default", "Default", "12a", "1 1111", "９９９９９", "99999"):
        payload = {"action": name, "scope": "GROUP", "user_id": "30001"}
        if bot is not None:
            payload["bot_self_id"] = bot
        assert (await action(dashboard_plugin, monkeypatch, **payload))["retcode"] == 403
    assert not await dashboard_plugin.db.fetchone("SELECT 1 FROM audit_log")


@pytest.mark.parametrize(("name", "changes"), [
    ("grant", {"user_id": "200xx"}),
    ("grant", {"scope": "all"}),
    ("grant", {"level_exempt": "sometimes"}),
    ("exemptions", {"membership_exempt": 2}),
    ("restore", {"scope": "all"}),
    ("ban", {"scope": "unknown"}),
    ("configure", {"primary_group_id": "some group"}),
    ("configure", {"group_to_private": "yes"}),
    ("configure", {"private_to_group": []}),
    ("configure", {"owner_bypass": 0}),
    ("configure", {"cache_ttl": 3601}),
    ("configure", {"cache_ttl": -1}),
    ("configure", {"cache_ttl": 1.5}),
    ("configure", {"group": {"min_level": 1001}}),
    ("configure", {"private": {"min_level": True}}),
    ("configure", {"group": {"approval_required": "false"}}),
    ("configure", {"private": {"unknown": 1}}),
    ("approve", {"application_id": "bad"}),
    ("reject", {"application_id": "L123456789abc", "note": "字" * 501}),
])
async def test_invalid_mutation_arguments_do_not_write(dashboard_plugin, monkeypatch, name, changes):
    result = await action(dashboard_plugin, monkeypatch, **(BASE | {"action": name} | changes))
    assert result["retcode"] == 400
    assert not await dashboard_plugin.db.fetchone("SELECT 1 FROM audit_log")


async def test_bounded_configuration_accepts_documented_values(dashboard_plugin, monkeypatch):
    result = await action(
        dashboard_plugin, monkeypatch, bot_self_id=BOT, action="configure", cache_ttl="3600",
        group_to_private=True, private_to_group="off", group={"min_level": "1000"},
        private={"min_level": 0, "approval_required": "off"},
    )
    assert result["retcode"] == 0, result
    state = await dashboard_plugin.scopes.overview(BOT)
    assert state["settings"]["cache_ttl"] == 3600
    assert state["settings"]["group_to_private"]
    assert not state["settings"]["private_to_group"]
    assert state["policies"]["GROUP"]["min_level"] == 1000
    assert not state["policies"]["PRIVATE"]["approval_required"]
    assert not (await dashboard_plugin.scopes.settings("22222")).group_to_private


async def test_dashboard_applies_all_restrictions_only_to_selected_bot(dashboard_plugin, monkeypatch):
    for name in ("ban", "revoke", "unban"):
        result = await action(dashboard_plugin, monkeypatch, **(BASE | {"action": name, "scope": "all"}))
        assert result["retcode"] == 0, result
    rows = await dashboard_plugin.db.fetchall("SELECT bot_self_id, scope, kind FROM grant_restrictions ORDER BY scope")
    assert [tuple(row) for row in rows] == [(BOT, "GROUP", "REVOKED"), (BOT, "PRIVATE", "REVOKED")]


async def test_dashboard_only_discovers_current_bots_and_validates_user_filter(dashboard_plugin, monkeypatch):
    monkeypatch.setattr(main, "request", Request("dashboard-admin", {"bot_self_id": BOT}))
    assert (await dashboard_plugin._web_bots())["data"]["verified_bots"] == [BOT, "22222"]
    main.request.query["user_id"] = "not a QQ"
    assert (await dashboard_plugin._web_state())["retcode"] == 400
