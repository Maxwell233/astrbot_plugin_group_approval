"""Security regressions retained from earlier implementation rounds."""

import asyncio

from approval.auth_service import AuthorizationService
from tests.test_scope_service import BOT, OWNER, PRIMARY


async def test_webui_auth_enforcement():
    from types import SimpleNamespace

    auth = AuthorizationService(lambda: "astrbot")
    assert auth.get_authenticated_user(SimpleNamespace(headers={"X-AstrBot-User": "astrbot"})) is None
    assert auth.get_authenticated_user(SimpleNamespace(username="api_key:abc123")) is None
    assert auth.get_authenticated_user(SimpleNamespace(username="astrbot")) == "astrbot"
    assert await auth.get_user_role("astrbot") == "OWNER"
    for name in ("admin", "root", "owner", "99999", "88888", "reviewer_bob"):
        assert await auth.get_user_role(name) == "DASHBOARD_USER"


async def test_concurrent_manual_grants_preserve_distinct_keys(scoped):
    service, napcat = scoped
    users = [str(30000 + i) for i in range(50)]
    for user in users:
        napcat.set_member(PRIMARY, user, "25")
    await asyncio.gather(*(service.manual_grant(BOT, user, "GROUP", OWNER) for user in users))
    rows = (await service.overview(BOT))["grants"]
    assert len(rows) == 50
    assert {r["user_id"] for r in rows} == set(users)
    assert all(r["source"] == "OWNER_MANUAL" for r in rows)
