"""Protocol, reviewer, and request-parsing regressions."""

from unittest.mock import MagicMock

import pytest

from approval.napcat_client import normalize_onebot_response
from approval.reviewers import ReviewerService
from main import GroupApprovalPlugin
from tests.test_scope_service import OWNER, REVIEWER


def test_napcat_normalization_formats():
    # Wrapped OneBot v11 format
    wrapped = {"status": "ok", "retcode": 0, "data": {"level": 15, "user_id": 123}}
    ok, data, err, code = normalize_onebot_response(wrapped)
    assert ok is True
    assert data["level"] == 15
    assert code == 0

    # Unpacked / direct dictionary format
    direct = {"message_id": 9999, "status": "sent"}
    ok2, data2, err2, code2 = normalize_onebot_response(direct)
    assert ok2 is True
    assert data2["message_id"] == 9999

    # OneBot error format
    err_resp = {"status": "failed", "retcode": 10001, "msg": "Member not in group"}
    ok3, data3, err3, code3 = normalize_onebot_response(err_resp)
    assert ok3 is False
    assert code3 == 10001
    assert "Member not in group" in err3

    # Empty or None response
    ok4, _, _, code4 = normalize_onebot_response(None)
    assert ok4 is False
    assert code4 == -4


def test_no_runtime_moderation_priority_mutation():
    plugin = GroupApprovalPlugin(context=MagicMock())
    assert not hasattr(plugin, "_promote_group_guardian_priority")
    assert not hasattr(plugin, "on_message_gate_priority_mutation")



async def test_transaction_rollback_on_error(temp_db):
    query = "INSERT INTO reviewer_overrides VALUES (?, 'ADDED', '99999', 1)"
    async with temp_db.transaction():
        await temp_db.execute(query, ("77777",))
        assert await temp_db.fetchone("SELECT 1 FROM reviewer_overrides WHERE qq_id = '77777'")
    with pytest.raises(RuntimeError):
        async with temp_db.transaction():
            await temp_db.execute(query, ("66666",))
            raise RuntimeError("test rollback")
    assert not await temp_db.fetchone("SELECT 1 FROM reviewer_overrides WHERE qq_id = '66666'")
    assert await temp_db.fetchone("SELECT 1 FROM reviewer_overrides WHERE qq_id = '77777'")


async def test_reviewer_persistence_in_db(temp_db, test_config):
    reviewers = ReviewerService(temp_db, test_config)
    assert await reviewers.can_review(REVIEWER)
    await reviewers.set_reviewer(REVIEWER, OWNER, enabled=False)
    await reviewers.set_reviewer("33333", OWNER, enabled=True)
    restarted = ReviewerService(temp_db, test_config)
    assert not await restarted.can_review(REVIEWER)
    assert await restarted.can_review("33333")
    assert await restarted.can_review(OWNER)
    assert await restarted.list_reviewers() == ["33333"]
