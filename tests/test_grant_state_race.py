"""Grant transitions must lose to a restriction written during the NapCat lookup."""

import asyncio

import pytest

from approval.rate_limit import RateLimiter
from approval.scope_service import ScopeError
from tests.test_scope_service import BOT, OWNER, PRIMARY, USER


@pytest.mark.parametrize("kind", ["REVOKED", "BLACKLIST"])
async def test_restriction_wins_during_level_check(scoped, monkeypatch, kind):
    service, _ = scoped
    await service.apply(BOT, USER, "GROUP", "")
    await service.configure(BOT, OWNER, cache_ttl=0)
    original = service._member_level

    async def pause(settings, user, *, force):
        result = await original(settings, user, force=force)
        await service.restrict(BOT, USER, ["GROUP"], OWNER, "OWNER", kind=kind)
        return result

    monkeypatch.setattr(service, "_member_level", pause)
    assert await service.check_chat(BOT, USER, "GROUP")
    assert (await service.grant(BOT, USER, "GROUP"))["status"] == ("REVOKED" if kind == "REVOKED" else "ACTIVE")
    with pytest.raises(ScopeError):
        await service.apply(BOT, USER, "GROUP", "")


async def test_concurrent_rate_limit_check_is_atomic(temp_db):
    limiter = RateLimiter(temp_db)
    decisions = await asyncio.gather(*(limiter.allow("test", 3, 60) for _ in range(10)))
    assert sum(decisions) == 3


async def test_rate_limit_renews_at_window_boundary(temp_db, monkeypatch):
    import approval.rate_limit as module

    now = 1000
    monkeypatch.setattr(module.time, "time", lambda: now)
    limiter = RateLimiter(temp_db)
    assert await limiter.allow("test", 1, 60)
    now += 59
    assert not await limiter.allow("test", 1, 60)
    now += 1
    assert await limiter.allow("test", 1, 60)


async def test_owner_bypass_disabled_during_permission_check_is_not_reused(scoped, monkeypatch):
    service, _ = scoped
    await service.configure(BOT, OWNER, owner_bypass=True)
    restricted = service._restricted
    changed = False

    async def update(*args, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            await service.configure(BOT, OWNER, owner_bypass=False)
        return await restricted(*args, **kwargs)

    monkeypatch.setattr(service, "_restricted", update)
    assert await service.check_chat(BOT, OWNER, "GROUP")
