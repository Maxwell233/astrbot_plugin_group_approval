"""Security regressions retained from earlier implementation rounds."""

import asyncio

from approval.auth_service import AuthorizationService
from approval.level_service import LevelService
from approval.napcat_client import NapCatClient
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


def test_post_type_filtering_only_identifies_chat_messages():
    from main import _is_chat_message

    event = object()
    assert not _is_chat_message(event, {"post_type": "notice"})
    assert not _is_chat_message(event, {"post_type": "request"})
    assert _is_chat_message(event, {"post_type": "message"})


def test_strict_level_pattern_matching():
    level_service = LevelService(NapCatClient())

    # Valid patterns
    assert level_service.parse_level(18) == 18
    assert level_service.parse_level("18") == 18
    assert level_service.parse_level("LV18") == 18
    assert level_service.parse_level("lv 18") == 18
    assert level_service.parse_level("等级18") == 18
    assert level_service.parse_level("Lv0") == 0

    # Invalid / messy text patterns that MUST NOT be matched
    assert level_service.parse_level("rank_999_user") is None
    assert level_service.parse_level("VIP1234") is None
    assert level_service.parse_level("LV") is None
    assert level_service.parse_level("hello 18 world") is None
    assert level_service.parse_level("-5") is None
    assert level_service.parse_level("") is None
    assert level_service.parse_level(None) is None


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
