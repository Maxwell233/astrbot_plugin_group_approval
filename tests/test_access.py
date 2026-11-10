"""Scoped access decisions and database persistence."""

from approval import AuditService, Database, LevelService, NotificationService, ReviewerService
from approval.scope_service import ScopeService
from tests.test_scope_service import BOT, OTHER_BOT, OWNER, PRIMARY, USER


async def test_access_requires_active_grant(scoped):
    service, _ = scoped
    assert await service.check_chat(BOT, USER, "GROUP")
    await service.apply(BOT, USER, "GROUP", PRIMARY)
    assert await service.check_chat(BOT, USER, "GROUP") is None


async def test_revocation_immediately_blocks_access(scoped):
    service, _ = scoped
    await service.apply(BOT, USER, "GROUP", PRIMARY)
    await service.restrict(BOT, USER, ["GROUP"], OWNER, "OWNER")
    assert await service.check_chat(BOT, USER, "GROUP")


async def test_blacklist_overrides_active_grant(scoped):
    service, _ = scoped
    await service.apply(BOT, USER, "GROUP", PRIMARY)
    await service.restrict(BOT, USER, ["GROUP"], OWNER, "OWNER", kind="BLACKLIST")
    assert (await service.grant(BOT, USER, "GROUP"))["status"] == "ACTIVE"
    assert await service.check_chat(BOT, USER, "GROUP")


async def test_group_chat_source_does_not_become_a_second_level_source(scoped):
    service, napcat = scoped
    napcat.set_member("10002", USER, "0")
    await service.apply(BOT, USER, "GROUP", "10002")
    assert await service.check_chat(BOT, USER, "GROUP") is None


async def test_cross_bot_id_isolation(scoped):
    service, _ = scoped
    await service.apply(BOT, USER, "GROUP", PRIMARY)
    assert await service.check_chat(BOT, USER, "GROUP") is None
    assert await service.check_chat(OTHER_BOT, USER, "GROUP")


async def test_persistence_across_restart(scoped):
    service, napcat = scoped
    await service.apply(BOT, USER, "GROUP", PRIMARY)
    db = Database(service.db.db_path)
    await db.init()
    try:
        reopened = ScopeService(
            db, LevelService(napcat), AuditService(db), NotificationService(db, napcat), service.config,
            ReviewerService(db, service.config),
        )
        assert await reopened.check_chat(BOT, USER, "GROUP") is None
    finally:
        await db.close()


async def test_level_drop_suspends_and_reapplication_restores(scoped):
    service, napcat = scoped
    await service.configure(BOT, OWNER, cache_ttl=0)
    await service.apply(BOT, USER, "GROUP", PRIMARY)
    napcat.set_member(PRIMARY, USER, "7")
    assert "需要 LV10" in await service.check_chat(BOT, USER, "GROUP")
    assert (await service.grant(BOT, USER, "GROUP"))["status"] == "SUSPENDED"
    napcat.set_member(PRIMARY, USER, "15")
    assert await service.check_chat(BOT, USER, "GROUP")
    assert "已恢复" in await service.apply(BOT, USER, "GROUP", PRIMARY)
    assert await service.check_chat(BOT, USER, "GROUP") is None
