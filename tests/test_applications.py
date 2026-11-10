"""Current application transitions and live member verification."""

import asyncio
from sqlite3 import IntegrityError

import pytest

from approval.scope_service import ScopeError
from tests.test_scope_service import BOT, OWNER, PRIMARY, REVIEWER, USER


async def test_apply_qualifying_level_creates_pending(scoped):
    service, napcat = scoped
    napcat.set_member(PRIMARY, USER, "25")
    await service.apply(BOT, USER, "PRIVATE", "10002", "request")
    app, = (await service.overview(BOT))["applications"]
    assert (app["level"], app["status"], app["reason"], app["source_group_id"]) == (25, "PENDING", "request", "10002")


async def test_apply_under_level_rejected(scoped):
    service, _ = scoped
    with pytest.raises(ScopeError, match="需要 LV20"):
        await service.apply(BOT, USER, "PRIVATE", "")
    assert not (await service.overview(BOT))["applications"]


async def test_duplicate_pending_prevented(scoped):
    service, napcat = scoped
    napcat.set_member(PRIMARY, USER, "25")
    await service.apply(BOT, USER, "PRIVATE", "")
    assert "已有" in await service.apply(BOT, USER, "PRIVATE", "")
    assert len((await service.overview(BOT))["applications"]) == 1


@pytest.mark.parametrize("approve", [True, False])
async def test_reviewer_can_approve_or_reject(scoped, approve):
    service, napcat = scoped
    napcat.set_member(PRIMARY, USER, "25")
    await service.apply(BOT, USER, "PRIVATE", "")
    app, = (await service.overview(BOT))["applications"]
    await service.review(BOT, app["id"], REVIEWER, "REVIEWER", approve=approve, note="test note")
    stored = await service.application(BOT, app["id"])
    assert stored["status"] == ("APPROVED" if approve else "REJECTED")
    assert stored["reviewer_id"] == REVIEWER
    assert stored["review_note"] == "test note"
    assert bool(await service.grant(BOT, USER, "PRIVATE")) is approve
    result = await service.db.fetchone("SELECT payload FROM notification_outbox WHERE event_key = ?", (
        f"LLM_RESULT_{app['id']}",
    ))
    assert "test note" in result["payload"]


async def test_blacklisted_applicant_cannot_be_approved(scoped):
    service, napcat = scoped
    napcat.set_member(PRIMARY, USER, "25")
    await service.apply(BOT, USER, "PRIVATE", "")
    app, = (await service.overview(BOT))["applications"]
    await service.restrict(BOT, USER, ["PRIVATE"], OWNER, "OWNER", kind="BLACKLIST")
    with pytest.raises(ScopeError):
        await service.review(BOT, app["id"], REVIEWER, "REVIEWER", approve=True)
    assert not await service.grant(BOT, USER, "PRIVATE")


@pytest.mark.parametrize(("exempt", "level", "is_member", "query_fails", "allowed"), [
    (False, "18", True, False, True),
    (False, "9", True, False, False),
    (False, None, True, False, False),
    (False, "18", False, False, False),
    (False, "18", True, True, False),
    (True, "5", True, False, True),
    (True, "18", False, False, False),
    (True, "18", True, True, False),
])
async def test_manual_grant_requires_fresh_membership_and_level(
    scoped, exempt, level, is_member, query_fails, allowed,
):
    service, napcat = scoped
    cached = await service._member_level(await service.settings(BOT), USER, force=True)
    assert cached == 15
    napcat.set_member(PRIMARY, USER, level=level, is_member=is_member)
    napcat.network_error_on_next = query_fails
    args = (BOT, USER, "GROUP", OWNER)
    if allowed:
        await service.manual_grant(*args, level_exempt=exempt)
        grant = await service.grant(BOT, USER, "GROUP")
        assert grant["status"] == "ACTIVE"
        assert grant["level_exempt"] == exempt
        assert (await service.overview(BOT))["levels"][0]["level"] == int(level)
    else:
        with pytest.raises(ScopeError):
            await service.manual_grant(*args, level_exempt=exempt)
        assert await service.grant(BOT, USER, "GROUP") is None
        assert not await service.db.fetchone("SELECT id FROM audit_log WHERE action = 'LLM_OWNER_GRANT'")


@pytest.mark.parametrize("scopes", [["GROUP"], ["GROUP", "PRIVATE"]])
async def test_unban_rolls_back_when_audit_insert_fails(scoped, scopes):
    service, _ = scoped
    await service.restrict(BOT, USER, scopes, OWNER, "OWNER", kind="BLACKLIST")
    await service.db.execute("""
        CREATE TRIGGER fail_unban_audit BEFORE INSERT ON audit_log
        WHEN NEW.action = 'LLM_RESTRICTION_UPDATED' AND json_extract(NEW.details_json, '$.removed') = 1
        BEGIN SELECT RAISE(ABORT, 'unban audit failed'); END
    """)
    with pytest.raises(IntegrityError, match="unban audit failed"):
        await service.restrict(BOT, USER, scopes, OWNER, "OWNER", kind="BLACKLIST", remove=True)
    for scope in scopes:
        assert await service._restricted(BOT, USER, scope)


async def test_manual_grant_does_not_join_an_inflight_cached_member_lookup(scoped, monkeypatch):
    service, napcat = scoped
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def query(*, no_cache, **kwargs):
        calls.append(no_cache)
        if no_cache:
            return {"status": "failed", "retcode": 10001}
        entered.set()
        await release.wait()
        return {"group_id": PRIMARY, "user_id": USER, "level": 30}

    monkeypatch.setattr(napcat, "get_group_member_info", query)
    old = asyncio.create_task(service.level.get_group_member_level(BOT, PRIMARY, USER))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(ScopeError):
            await asyncio.wait_for(service.manual_grant(BOT, USER, "GROUP", OWNER), 2)
        assert calls == [False, True]
        assert not await service.grant(BOT, USER, "GROUP")
    finally:
        release.set()
        await asyncio.wait_for(old, 2)


@pytest.mark.parametrize("reply", [
    {"user_id": 20001, "group_id": 10001, "level": "20", "is_member": False},
    {"group_id": 10001, "level": "20"},
    {"user_id": 20001, "level": "20"},
])
async def test_exempt_manual_grant_requires_reply_to_verify_target(scoped, monkeypatch, reply):
    service, napcat = scoped

    async def lookup(**kwargs):
        return reply

    monkeypatch.setattr(napcat, "get_group_member_info", lookup)
    with pytest.raises(ScopeError):
        await service.manual_grant(BOT, USER, "GROUP", OWNER, level_exempt=True)
    assert not await service.grant(BOT, USER, "GROUP")
