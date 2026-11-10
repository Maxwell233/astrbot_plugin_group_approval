"""Live SQLite, wrapped OneBot-shaped responses, and dual-scope authorization."""

import asyncio

import pytest

from approval.scope_commands import ScopeCommands
from approval.scope_service import ScopeError, ScopeService

BOT, OTHER_BOT, PRIMARY, OTHER_GROUP, USER = "11111", "22222", "10001", "10002", "20001"
OWNER, REVIEWER = "99999", "88888"


@pytest.fixture
async def scoped(access_system):
    _, legacy, db, napcat, config = access_system
    service = ScopeService(db, legacy.level_service, legacy.audit, legacy.notifications, config, legacy)
    napcat.set_member(PRIMARY, BOT, "30")
    napcat.set_member(PRIMARY, OTHER_BOT, "30")
    napcat.set_member(PRIMARY, USER, "15")
    await service.configure(BOT, OWNER, primary_group_id=PRIMARY, policies={"GROUP": {"approval_required": False}})
    return service, napcat


async def test_group_is_bot_wide_and_scope_isolated(scoped):
    service, napcat = scoped
    napcat.set_member(OTHER_GROUP, USER, "0")
    assert "已开通" in await service.apply(BOT, USER, "GROUP", OTHER_GROUP)
    assert await service.check_chat(BOT, USER, "GROUP") is None
    assert await service.check_chat(BOT, USER, "PRIVATE")
    assert await service.check_chat(OTHER_BOT, USER, "GROUP")
    with pytest.raises(ScopeError, match="LV20"):
        await service.apply(BOT, USER, "PRIVATE", "")
    assert not (await service.overview(BOT))["applications"]


async def test_private_approval_is_independent_and_refreshes_at_approval(scoped):
    service, napcat = scoped
    napcat.set_member(PRIMARY, USER, "25")
    await service.apply(BOT, USER, "PRIVATE", "")
    app = (await service.overview(BOT))["applications"][0]
    assert app["status"] == "PENDING"
    napcat.set_member(PRIMARY, USER, "15")
    with pytest.raises(ScopeError, match="LV20"):
        await service.review(BOT, app["id"], REVIEWER, "REVIEWER", approve=True)
    napcat.set_member(PRIMARY, USER, "25")
    await service.review(BOT, app["id"], REVIEWER, "REVIEWER", approve=True)
    assert await service.check_chat(BOT, USER, "PRIVATE") is None
    assert await service.check_chat(BOT, USER, "GROUP")
    assert (await service.grant(BOT, USER, "PRIVATE"))["source"] == "MANUAL_APPROVAL"


@pytest.mark.parametrize(("source", "target", "direction"), [
    ("GROUP", "PRIVATE", "group_to_private"), ("PRIVATE", "GROUP", "private_to_group"),
])
async def test_cross_grant_requires_target_application_and_correct_direction(scoped, source, target, direction):
    service, napcat = scoped
    napcat.set_member(PRIMARY, USER, "25")
    await service.configure(BOT, OWNER, policies={"GROUP": {"approval_required": True}})
    await service.manual_grant(BOT, USER, source, OWNER, level_exempt=True, membership_exempt=True)
    assert await service.check_chat(BOT, USER, target)
    await service.apply(BOT, USER, target, OTHER_GROUP)
    assert (await service.overview(BOT))["applications"][0]["status"] == "PENDING"
    await service.configure(BOT, OWNER, **{direction: True})
    assert await service.grant(BOT, USER, target) is None
    await service.apply(BOT, USER, target, OTHER_GROUP)
    grant = await service.grant(BOT, USER, target)
    assert grant["source"] == "CROSS_GRANT"
    assert not grant["level_exempt"] and not grant["membership_exempt"]
    await service.restrict(BOT, USER, [target], REVIEWER, "REVIEWER")
    with pytest.raises(ScopeError, match="撤销"):
        await service.apply(BOT, USER, target, OTHER_GROUP)
    assert await service.check_chat(BOT, USER, source) is None
    await service.restrict(BOT, USER, ["GROUP", "PRIVATE"], REVIEWER, "REVIEWER")
    assert (await service.grant(BOT, USER, source))["status"] == "REVOKED"


async def test_level_drop_and_failed_queries_do_not_create_or_permanently_revoke(scoped):
    service, napcat = scoped
    await service.configure(BOT, OWNER, cache_ttl=0)
    await service.apply(BOT, USER, "GROUP", "")
    napcat.set_member(PRIMARY, USER, "25")
    await service.manual_grant(BOT, USER, "PRIVATE", OWNER)
    napcat.set_member(PRIMARY, USER, "15")
    assert "需要 LV20" in await service.check_chat(BOT, USER, "PRIVATE")
    assert (await service.grant(BOT, USER, "PRIVATE"))["status"] == "SUSPENDED"
    assert await service.check_chat(BOT, USER, "GROUP") is None
    napcat.set_member(PRIMARY, USER, "25", is_member=False)
    assert await service.check_chat(BOT, USER, "GROUP")
    assert (await service.grant(BOT, USER, "GROUP"))["status"] != "REVOKED"
    napcat.set_member(PRIMARY, USER, "25")
    assert await service.check_chat(BOT, USER, "PRIVATE")
    assert "已恢复" in await service.apply(BOT, USER, "PRIVATE", "")
    assert await service.check_chat(BOT, USER, "PRIVATE") is None


async def test_primary_change_checks_bot_and_invalidates_old_snapshots(scoped):
    service, napcat = scoped
    await service.apply(BOT, USER, "GROUP", "")
    await service.restrict(BOT, USER, ["PRIVATE"], OWNER, "OWNER")
    old = await service.settings(BOT)
    with pytest.raises(ScopeError, match="新主群"):
        await service.configure(BOT, OWNER, primary_group_id=OTHER_GROUP)
    napcat.set_member(OTHER_GROUP, BOT, "25")
    napcat.set_member(OTHER_GROUP, USER, "1")
    new = await service.configure(BOT, OWNER, primary_group_id=OTHER_GROUP)
    assert new.primary_revision == old.primary_revision + 1
    assert not (await service.overview(BOT))["levels"]
    assert "LV1" in await service.check_chat(BOT, USER, "GROUP")
    assert await service.check_chat(BOT, USER, "PRIVATE")
    assert (await service.grant(BOT, USER, "GROUP"))["status"] == "SUSPENDED"


async def test_owner_exemptions_are_independent_and_removable(scoped):
    service, napcat = scoped
    await service.manual_grant(BOT, USER, "PRIVATE", OWNER, level_exempt=True)
    assert await service.check_chat(BOT, USER, "PRIVATE") is None
    await service.configure(BOT, OWNER, cache_ttl=0)
    napcat.set_member(PRIMARY, USER, "25", is_member=False)
    assert await service.check_chat(BOT, USER, "PRIVATE")
    with pytest.raises(ScopeError):
        await service.manual_grant(BOT, USER, "PRIVATE", OWNER, membership_exempt=True)
    await service.manual_grant(BOT, USER, "PRIVATE", OWNER, membership_exempt=True, level_exempt=True)
    assert await service.check_chat(BOT, USER, "PRIVATE") is None
    await service.set_exemptions(BOT, USER, "PRIVATE", OWNER, membership_exempt=False, level_exempt=False)
    assert (await service.grant(BOT, USER, "PRIVATE"))["status"] == "SUSPENDED"
    with pytest.raises(ScopeError, match="权限"):
        await ScopeCommands(service).action(BOT, REVIEWER, "REVIEWER", "grant", {
            "user_id": USER, "scope": "PRIVATE", "membership_exempt": True, "level_exempt": True,
        })


async def test_administrators_do_not_automatically_get_chat_permissions(scoped):
    service, _napcat = scoped
    commands = ScopeCommands(service)
    assert await service.check_chat(BOT, OWNER, "GROUP")
    assert await service.check_chat(BOT, REVIEWER, "PRIVATE")
    assert "主群" in await commands.admin(BOT, REVIEWER, True, ["status"])
    assert "私聊" in await commands.admin(BOT, REVIEWER, False, ["pending"])
    service.config.private_chat.admin_commands_enabled = False
    assert "关闭" in await commands.admin(BOT, OWNER, True, ["status"])


async def test_concurrent_application_and_review_are_idempotent(scoped):
    service, napcat = scoped
    service.config.application.query_limit = 5
    napcat.set_member(PRIMARY, USER, "25")
    await service.configure(BOT, OWNER, policies={"GROUP": {"approval_required": True}})
    await asyncio.gather(*(service.apply(BOT, USER, "PRIVATE", "") for _ in range(5)))
    await service.apply(BOT, USER, "GROUP", OTHER_GROUP)
    pending = (await service.overview(BOT))["applications"]
    assert {row["scope"] for row in pending} == {"GROUP", "PRIVATE"}
    assert len(pending) == 2
    app = next(a for a in pending if a["scope"] == "PRIVATE")
    with pytest.raises(ScopeError, match="该 Bot"):
        await service.review(OTHER_BOT, app["id"], REVIEWER, "REVIEWER", approve=True)
    outcomes = await asyncio.gather(
        *(service.review(BOT, app["id"], REVIEWER, "REVIEWER", approve=True) for _ in range(2)),
        return_exceptions=True,
    )
    assert sum(isinstance(result, str) for result in outcomes) == 1
    assert len((await service.overview(BOT))["grants"]) == 1


async def test_revoke_during_restore_prevents_resurrection(scoped, monkeypatch):
    service, napcat = scoped
    await service.configure(BOT, OWNER, cache_ttl=0)
    await service.apply(BOT, USER, "GROUP", "")
    napcat.set_member(PRIMARY, USER, "1")
    assert await service.check_chat(BOT, USER, "GROUP")
    napcat.set_member(PRIMARY, USER, "15")
    lookup = service._member_level

    async def revoke_during_lookup(*args, **kwargs):
        result = await lookup(*args, **kwargs)
        await service.restrict(BOT, USER, ["GROUP"], OWNER, "OWNER")
        return result

    monkeypatch.setattr(service, "_member_level", revoke_during_lookup)
    with pytest.raises(ScopeError, match="撤销"):
        await service.apply(BOT, USER, "GROUP", "")
    assert (await service.grant(BOT, USER, "GROUP"))["status"] == "REVOKED"


async def test_audit_and_outbox_failure_rolls_back_approval(scoped, monkeypatch):
    service, napcat = scoped
    napcat.set_member(PRIMARY, USER, "25")
    await service.apply(BOT, USER, "PRIVATE", "")
    app = (await service.overview(BOT))["applications"][0]
    enqueue = service.notifications.enqueue

    async def broken(*args, **kwargs):
        await enqueue(*args, **kwargs)
        raise RuntimeError("queue failed")

    monkeypatch.setattr(service.notifications, "enqueue", broken)
    with pytest.raises(RuntimeError, match="queue failed"):
        await service.review(BOT, app["id"], REVIEWER, "REVIEWER", approve=True)
    assert (await service.application(BOT, app["id"]))["status"] == "PENDING"
    assert await service.grant(BOT, USER, "PRIVATE") is None
    assert not await service.db.fetchone("SELECT id FROM audit_log WHERE action = 'LLM_APPLICATION_APPROVED'")
    assert not await service.db.fetchone("SELECT id FROM notification_outbox WHERE event_key = ?", (f"LLM_RESULT_{app['id']}",))


async def test_legacy_grant_does_not_authorize_any_scope(scoped):
    service, _napcat = scoped
    assert (await service.legacy.grant_manually(OWNER, "OWNER", BOT, PRIMARY, USER))[0]
    assert await service.check_chat(BOT, USER, "GROUP")
    assert await service.check_chat(BOT, USER, "PRIVATE")
    assert await service.db.fetchone("SELECT 1 FROM grants WHERE bot_self_id = ? AND user_id = ?", (BOT, USER))


async def test_cross_grant_does_not_bypass_target_level_or_membership(scoped):
    service, napcat = scoped
    await service.configure(BOT, OWNER, group_to_private=True)
    await service.manual_grant(BOT, USER, "GROUP", OWNER, level_exempt=True, membership_exempt=True)
    with pytest.raises(ScopeError, match="LV20"):
        await service.apply(BOT, USER, "PRIVATE", "")
    napcat.set_member(PRIMARY, USER, "25", is_member=False)
    with pytest.raises(ScopeError):
        await service.apply(BOT, USER, "PRIVATE", "")
    assert await service.grant(BOT, USER, "PRIVATE") is None


async def test_main_group_change_during_a_live_check_denies_old_response(scoped, monkeypatch):
    service, napcat = scoped
    await service.configure(BOT, OWNER, cache_ttl=0)
    await service.apply(BOT, USER, "GROUP", "")
    napcat.set_member(OTHER_GROUP, BOT, "30")
    lookup = service.level.get_group_member_level

    async def change_main(bot_id, group_id, user_id, **kwargs):
        result = await lookup(bot_id, group_id, user_id, **kwargs)
        if user_id == USER and group_id == PRIMARY:
            await service.configure(BOT, OWNER, primary_group_id=OTHER_GROUP)
        return result

    monkeypatch.setattr(service.level, "get_group_member_level", change_main)
    assert "已变更" in await service.check_chat(BOT, USER, "GROUP")
    assert not (await service.overview(BOT))["levels"]


@pytest.mark.parametrize("prior", ["ACTIVE", "SUSPENDED", "REVOKED", None])
async def test_blacklist_preserves_grant_and_unban_requires_current_level(scoped, prior):
    service, napcat = scoped
    if prior:
        await service.apply(BOT, USER, "GROUP", "")
        if prior == "SUSPENDED":
            await service.db.execute("UPDATE grants_v2 SET status = 'SUSPENDED' WHERE bot_self_id = ?", (BOT,))
        if prior == "REVOKED":
            await service.restrict(BOT, USER, ["GROUP"], REVIEWER, "REVIEWER")
    await service.restrict(BOT, USER, ["GROUP"], OWNER, "OWNER", kind="BLACKLIST")
    grant = await service.grant(BOT, USER, "GROUP")
    assert (grant["status"] if grant else None) == prior
    assert await service.check_chat(BOT, USER, "GROUP")
    with pytest.raises(ScopeError, match="黑名单"):
        await service.apply(BOT, USER, "GROUP", "")
    await service.restrict(BOT, USER, ["GROUP"], OWNER, "OWNER", kind="BLACKLIST", remove=True)
    grant = await service.grant(BOT, USER, "GROUP")
    assert (grant["status"] if grant else None) == prior
    if prior == "ACTIVE":
        napcat.set_member(PRIMARY, USER, "25")
        assert await service.check_chat(BOT, USER, "GROUP") is None
        assert (await service.overview(BOT))["levels"][0]["level"] == 25
    else:
        assert await service.check_chat(BOT, USER, "GROUP")


async def test_blacklist_all_scope_and_failed_audit_rollback(scoped, monkeypatch):
    service, napcat = scoped
    napcat.set_member(PRIMARY, USER, "25")
    await service.apply(BOT, USER, "GROUP", "")
    await service.apply(BOT, USER, "PRIVATE", "")
    await service.restrict(BOT, USER, ["GROUP"], OWNER, "OWNER", kind="BLACKLIST")
    assert await service.check_chat(BOT, USER, "GROUP")
    app = (await service.overview(BOT))["applications"][0]
    assert app["status"] == "PENDING"
    original = service.audit.log

    async def fail_private(**kwargs):
        await original(**kwargs)
        if kwargs["details"]["scope"] == "PRIVATE":
            raise RuntimeError("audit failed")

    with monkeypatch.context() as patch:
        patch.setattr(service.audit, "log", fail_private)
        with pytest.raises(RuntimeError):
            await service.restrict(BOT, USER, ["GROUP", "PRIVATE"], OWNER, "OWNER", kind="BLACKLIST")
    assert (await service.application(BOT, app["id"]))["status"] == "PENDING"
    assert not await service._restricted(BOT, USER, "PRIVATE")
    await service.restrict(BOT, USER, ["GROUP", "PRIVATE"], OWNER, "OWNER", kind="BLACKLIST")
    assert await service._restricted(BOT, USER, "PRIVATE")
    assert (await service.application(BOT, app["id"]))["status"] == "CANCELLED"
    await service.restrict(BOT, USER, ["GROUP", "PRIVATE"], OWNER, "OWNER", kind="BLACKLIST", remove=True)
    assert await service.check_chat(BOT, USER, "GROUP") is None
    assert await service.check_chat(BOT, USER, "PRIVATE")


@pytest.mark.parametrize("network_error", [False, True])
async def test_ineligible_and_failed_queries_consume_pre_network_rate_limit(scoped, monkeypatch, network_error):
    service, napcat = scoped
    await service.configure(OTHER_BOT, OWNER, primary_group_id=PRIMARY)
    original = napcat.get_group_member_info
    calls = []

    async def record(**kwargs):
        calls.append(kwargs)
        if network_error:
            return {"status": "failed", "retcode": 100}
        return await original(**kwargs)

    monkeypatch.setattr(napcat, "get_group_member_info", record)
    napcat.set_member(PRIMARY, USER, "0")
    for _ in range(8):
        with pytest.raises(ScopeError):
            await service.apply(BOT, USER, "GROUP", "")
    assert len(calls) == 3
    for bot, scope in [(BOT, "PRIVATE"), (OTHER_BOT, "GROUP")]:
        with pytest.raises(ScopeError):
            await service.apply(bot, USER, scope, "")
    assert len(calls) == 5


async def test_pending_and_active_repeat_without_network(scoped, monkeypatch):
    service, napcat = scoped
    napcat.set_member(PRIMARY, USER, "25")
    await service.apply(BOT, USER, "GROUP", "")
    pending = await service.apply(BOT, USER, "PRIVATE", "")

    async def unexpected(**kwargs):
        pytest.fail("repeat application should not query NapCat")

    monkeypatch.setattr(napcat, "get_group_member_info", unexpected)
    assert "已有" in await service.apply(BOT, USER, "GROUP", "")
    assert pending.split("编号 ")[1].rstrip("。") in await service.apply(BOT, USER, "PRIVATE", "")


async def test_concurrent_failed_applications_cannot_exceed_query_limit(scoped, monkeypatch):
    service, napcat = scoped
    calls = 0

    async def fail(**kwargs):
        nonlocal calls
        calls += 1
        return {"status": "failed", "retcode": 100}

    monkeypatch.setattr(napcat, "get_group_member_info", fail)
    outcomes = await asyncio.gather(
        *(service.apply(BOT, USER, "GROUP", "") for _ in range(15)), return_exceptions=True
    )
    assert all(isinstance(result, ScopeError) for result in outcomes)
    assert calls == 3
    assert sum("频繁" in str(result) for result in outcomes) == 12


async def test_hourly_limit_only_charges_new_pending_and_remains_independent(scoped, monkeypatch):
    service, napcat = scoped
    service.config.application.rate_limit_per_hour = 1
    service.config.application.query_limit = 4
    napcat.set_member(PRIMARY, USER, "25")
    first = await service.apply(BOT, USER, "PRIVATE", "")
    assert "已有" in await service.apply(BOT, USER, "PRIVATE", "")
    await service.cancel(BOT, USER, "PRIVATE")
    with pytest.raises(ScopeError, match="过于频繁"):
        await service.apply(BOT, USER, "PRIVATE", "")
    assert "已开通" in await service.apply(BOT, USER, "GROUP", "")
    await service.configure(BOT, OWNER, group_to_private=True)
    assert "已开通" in await service.apply(BOT, USER, "PRIVATE", "")
    assert (await service.grant(BOT, USER, "PRIVATE"))["source"] == "CROSS_GRANT"
    assert first.startswith("私聊申请已提交")
