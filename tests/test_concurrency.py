"""Race tests against the real scoped SQLite service."""

import asyncio

from tests.test_scope_service import BOT, OWNER, PRIMARY, REVIEWER, USER


async def test_concurrent_approval_atomic(scoped):
    service, napcat = scoped
    napcat.set_member(PRIMARY, USER, "25")
    await service.apply(BOT, USER, "PRIVATE", PRIMARY)
    app, = (await service.overview(BOT))["applications"]
    results = await asyncio.gather(*(
        service.review(BOT, app["id"], REVIEWER, "REVIEWER", approve=True) for _ in range(10)
    ), return_exceptions=True)
    assert sum(isinstance(r, str) for r in results) == 1
    assert len((await service.overview(BOT))["grants"]) == 1
    assert (await service.db.fetchone("SELECT count(*) FROM audit_log WHERE action = 'LLM_APPLICATION_APPROVED'"))[0] == 1


async def test_concurrent_application_submission(scoped):
    service, _ = scoped
    service.config.application.query_limit = 10
    await service.configure(BOT, OWNER, policies={"GROUP": {"approval_required": True}})
    results = await asyncio.gather(*(service.apply(BOT, USER, "GROUP", PRIMARY) for _ in range(10)))
    assert sum("已提交" in r for r in results) == 1
    assert len((await service.overview(BOT))["applications"]) == 1
    assert (await service.db.fetchone("SELECT count(*) FROM audit_log WHERE action = 'LLM_APPLICATION_SUBMITTED'"))[0] == 1
