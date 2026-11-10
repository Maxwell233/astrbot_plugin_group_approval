"""Tests for NapCat group member level query service and parsing."""

from __future__ import annotations

import asyncio

import pytest

from approval.level_service import LevelService
from tests.conftest import MockNapCatClient


@pytest.mark.asyncio
async def test_level_parsing_formats(mock_napcat: MockNapCatClient):
    svc = LevelService(mock_napcat)

    assert svc.parse_level(15) == 15
    assert svc.parse_level("18") == 18
    assert svc.parse_level("LV18") == 18
    assert svc.parse_level("lv25") == 25
    assert svc.parse_level("lv 18") == 18
    assert svc.parse_level("Lv0") == 0
    assert svc.parse_level("等级30") == 30
    assert svc.parse_level("0") == 0
    assert svc.parse_level("") is None
    assert svc.parse_level(None) is None
    assert svc.parse_level("invalid_string") is None
    for invalid in (-5, "-5", "rank_999_user", "VIP1234", "LV", "hello 18 world", True, False):
        assert svc.parse_level(invalid) is None


@pytest.mark.asyncio
async def test_member_level_comparison(mock_napcat: MockNapCatClient):
    svc = LevelService(mock_napcat)

    # 1. LV9 in group with LV10 requirement
    res_lv9 = await svc.get_group_member_level("bot1", "10001", "20002")
    assert res_lv9.success is True
    assert res_lv9.level == 9

    # 2. LV10 in group with LV10 requirement
    res_lv10 = await svc.get_group_member_level("bot1", "10001", "20001")
    assert res_lv10.success is True
    assert res_lv10.level == 10

    # 3. LV20+ in group with LV10 requirement
    res_lv25 = await svc.get_group_member_level("bot1", "10001", "20003")
    assert res_lv25.success is True
    assert res_lv25.level == 25


@pytest.mark.asyncio
async def test_qq_level_vs_group_level(mock_napcat: MockNapCatClient):
    # 7. QQ 账号等级很高 (qq_level=120) 但群等级低 (level="5")
    mock_napcat.set_member(
        group_id="10001",
        user_id="20099",
        level="5",
        role="member",
        qq_level=120,
    )
    svc = LevelService(mock_napcat)

    res = await svc.get_group_member_level("bot1", "10001", "20099")
    assert res.success is True
    assert res.level == 5  # Strictly reads 'level', NEVER 'qq_level'


@pytest.mark.asyncio
async def test_missing_or_invalid_level(mock_napcat: MockNapCatClient):
    # 5. 群等级缺失
    mock_napcat.set_member(group_id="10001", user_id="20004", level=None)
    # 6. 群等级非法
    mock_napcat.set_member(group_id="10001", user_id="20005", level="unknown_text")

    svc = LevelService(mock_napcat)

    res_missing = await svc.get_group_member_level("bot1", "10001", "20004")
    assert res_missing.success is False
    assert res_missing.error_code == "INVALID_LEVEL"

    res_invalid = await svc.get_group_member_level("bot1", "10001", "20005")
    assert res_invalid.success is False
    assert res_invalid.error_code == "INVALID_LEVEL"


@pytest.mark.asyncio
async def test_timeout_handling(mock_napcat: MockNapCatClient):
    # 8. NapCat 超时
    mock_napcat.timeout_on_next = True
    svc = LevelService(mock_napcat)

    res = await svc.get_group_member_level("bot1", "10001", "20001")
    assert res.success is False
    assert "RETCODE" in res.error_code or "TIMEOUT" in res.error_code


@pytest.mark.asyncio
async def test_left_group_member(mock_napcat: MockNapCatClient):
    # 9. 已退群成员
    mock_napcat.set_member(group_id="10001", user_id="20006", is_member=False)
    svc = LevelService(mock_napcat)

    res = await svc.get_group_member_level("bot1", "10001", "20006")
    assert res.success is False
    assert res.is_member is False


@pytest.mark.asyncio
async def test_identity_mismatch_check():
    # 10. 查询返回的群号与QQ号不符
    class MismatchNapCat(MockNapCatClient):
        async def get_group_member_info(self, group_id, user_id, bot_self_id=None, no_cache=True):
            return True, {"group_id": 999999, "user_id": int(user_id), "level": "15"}, "", 0

    svc = LevelService(MismatchNapCat())
    res = await svc.get_group_member_level("bot1", "10001", "20001")
    assert res.success is False
    assert res.error_code == "GROUP_ID_MISMATCH"


@pytest.mark.asyncio
async def test_concurrent_request_coalescing(mock_napcat: MockNapCatClient):
    call_count = 0
    orig_get = mock_napcat.get_group_member_info

    async def counting_get(*args, **kwargs):
        nonlocal call_count
        assert kwargs["bot_self_id"] == "bot1"
        assert kwargs["no_cache"] is True
        call_count += 1
        await asyncio.sleep(0.05)  # simulate network latency
        return await orig_get(*args, **kwargs)

    mock_napcat.get_group_member_info = counting_get  # type: ignore
    svc = LevelService(mock_napcat)

    # Launch 5 concurrent queries for the exact same user
    tasks = [
        svc.get_group_member_level("bot1", "10001", "20001")
        for _ in range(5)
    ]
    results = await asyncio.gather(*tasks)

    # All should receive valid result, and remote was queried only once
    for r in results:
        assert r.success is True
        assert r.level == 10
    assert call_count == 1


async def test_cancelled_leader_resolves_shared_lookup_for_followers(monkeypatch, mock_napcat):
    service = LevelService(mock_napcat)
    entered = asyncio.Event()

    async def blocked(**kwargs):
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr(mock_napcat, "get_group_member_info", blocked)
    leader = asyncio.create_task(service.get_group_member_level("bot1", "10001", "20001"))
    await asyncio.wait_for(entered.wait(), 2)
    follower = asyncio.create_task(service.get_group_member_level("bot1", "10001", "20001"))
    await asyncio.sleep(0)
    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader
    result = await asyncio.wait_for(follower, 2)
    assert not result.success and result.error_code == "QUERY_CANCELLED"
