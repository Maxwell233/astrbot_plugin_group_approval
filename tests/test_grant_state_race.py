"""Regression tests: authorization state transitions under real concurrency.

Covers the P0 defect "administrator revoke loses to a concurrent group-level check":

    A: reads grant (ACTIVE) -> looks up level (await)
    B: administrator revokes -> REVOKED
    A: unconditional UPDATE -> SUSPENDED   (old code: destroys the REVOKED state)
    A: next level check sees SUSPENDED and the automatic restore policy revives it

The interleaving point is the real ``LevelService.get_group_member_level`` await inside
``AccessService.check_access``. ``PausingLevelService`` pauses there so the revoke is
guaranteed to commit between the read and the write - no artificial reordering of the
two functions.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from approval.access_service import AccessService
from approval.approval_service import ApprovalService
from approval.audit_service import AuditService
from approval.config import PluginConfig
from approval.database import Database
from approval.level_service import LevelService
from approval.models import GroupMemberLevelResult
from approval.notification_service import NotificationService


class PausingLevelService(LevelService):
    """Real LevelService interface, but able to pause inside the lookup await."""

    def __init__(self, client) -> None:
        super().__init__(client)
        self.level = 5
        self.is_member = True
        self.success = True
        self.pause_event: asyncio.Event | None = None
        self.release_event: asyncio.Event | None = None

    async def get_group_member_level(
        self, bot_id: str, group_id: str, user_id: str, force_refresh: bool = False
    ) -> GroupMemberLevelResult:
        result = GroupMemberLevelResult(
            success=self.success,
            level=self.level,
            is_member=self.is_member,
            group_id=str(group_id),
            user_id=str(user_id),
        )
        if self.pause_event is not None and self.release_event is not None:
            pause, release = self.pause_event, self.release_event
            self.pause_event = None  # pause only once
            pause.set()
            await asyncio.wait_for(release.wait(), timeout=10)
        return result


class _Client:
    """Minimal NapCat client stand-in; level lookups are served by PausingLevelService."""


CONFIG = {
    "enabled": True,
    "default_policy": "deny",
    "owner_qq_ids": ["99999"],
    "reviewer_qq_ids": ["88888"],
    "groups": {
        "10001": {
            "enabled": True,
            "min_level": 10,
            "approval_required": True,
            "grant_duration_days": 0,
            "recheck_interval_seconds": 1,
            "restore_policy": "automatic",
            "owner_bypass": True,
            "group_admin_bypass": False,
        }
    },
    "application": {"rate_limit_per_hour": 3},
    "notifications": {"fallback_group_id": ""},
}


@pytest.fixture
async def system(tmp_path):
    db = Database(tmp_path / "grants.db")
    await db.init()
    config = PluginConfig.from_dict(CONFIG)
    level = PausingLevelService(_Client())
    audit = AuditService(db)
    notif = NotificationService(db, _Client())
    approval = ApprovalService(db, level, audit, notif, config)
    access = AccessService(db, approval, level, audit, config)
    try:
        yield db, approval, access, level, config
    finally:
        await db.close()


async def _insert_active_grant(db: Database, user_id: str = "30005", verified_at: int = 0) -> None:
    now = int(time.time())
    await db.execute(
        """
        INSERT INTO grants (
            id, bot_self_id, group_id, user_id, status, approved_by, approved_at,
            expires_at, last_verified_level, last_verified_at
        ) VALUES (?, 'bot1', '10001', ?, 'ACTIVE', '99999', ?, 0, 20, ?)
        """,
        (f"G_{user_id}", user_id, now, verified_at),
    )
    await db.commit()


async def _grant_row(db: Database, user_id: str = "30005"):
    return await db.fetchone(
        "SELECT status, suspend_reason FROM grants WHERE bot_self_id='bot1' "
        "AND group_id='10001' AND user_id=?",
        (user_id,),
    )


async def test_revoke_during_level_check_cannot_be_downgraded_and_revived(system):
    """The full exploit chain: revoke -> downgraded to SUSPENDED -> auto-restored."""
    db, approval, access, level, _ = system
    await _insert_active_grant(db)

    pause, release = asyncio.Event(), asyncio.Event()
    level.pause_event, level.release_event = pause, release
    level.level = 5  # below min_level => triggers the suspension branch

    check = asyncio.create_task(access.check_access("bot1", "10001", "30005"))
    await asyncio.wait_for(pause.wait(), timeout=5)

    # Administrator revokes while the level lookup is in flight.
    ok, msg = await approval.revoke_grant("99999", "OWNER", "bot1", "10001", "30005", "abuse")
    assert ok is True, msg

    release.set()
    decision = await asyncio.wait_for(check, timeout=5)
    assert decision.allowed is False

    row = await _grant_row(db)
    assert row["status"] == "REVOKED", (
        f"level check overwrote administrator revoke (status={row['status']!r})"
    )

    # Now the user's level "recovers". A REVOKED grant must never be auto-restored.
    level.pause_event = level.release_event = None
    level.level = 50
    decision2 = await asyncio.wait_for(access.check_access("bot1", "10001", "30005"), timeout=5)
    assert decision2.allowed is False, "revoked grant was revived by the automatic restore policy"
    row2 = await _grant_row(db)
    assert row2["status"] == "REVOKED"


async def test_ban_during_level_check_keeps_revoked_state(system):
    db, approval, access, level, _ = system
    await _insert_active_grant(db, user_id="30006")

    pause, release = asyncio.Event(), asyncio.Event()
    level.pause_event, level.release_event = pause, release
    level.level = 5

    check = asyncio.create_task(access.check_access("bot1", "10001", "30006"))
    await asyncio.wait_for(pause.wait(), timeout=5)
    ok, msg = await approval.ban_user("99999", "OWNER", "bot1", "10001", "30006", "spam")
    assert ok is True, msg
    release.set()
    decision = await asyncio.wait_for(check, timeout=5)
    assert decision.allowed is False

    row = await _grant_row(db, "30006")
    assert row["status"] == "REVOKED", f"level check overwrote ban (status={row['status']!r})"

    level.pause_event = level.release_event = None
    level.level = 50
    decision2 = await asyncio.wait_for(access.check_access("bot1", "10001", "30006"), timeout=5)
    assert decision2.allowed is False
    assert (await _grant_row(db, "30006"))["status"] == "REVOKED"


async def test_expired_grant_cannot_be_revived(system):
    db, approval, access, level, _ = system
    now = int(time.time())
    await db.execute(
        """
        INSERT INTO grants (
            id, bot_self_id, group_id, user_id, status, approved_by, approved_at,
            expires_at, last_verified_level, last_verified_at
        ) VALUES ('G_EXP', 'bot1', '10001', '30007', 'ACTIVE', '99999', ?, ?, 20, 0)
        """,
        (now - 1000, now - 100),
    )
    await db.commit()

    level.level = 50
    decision = await asyncio.wait_for(access.check_access("bot1", "10001", "30007"), timeout=5)
    assert decision.allowed is False
    assert decision.reason == "GRANT_EXPIRED"
    assert (await _grant_row(db, "30007"))["status"] == "EXPIRED"

    # A manual revoke must not resurrect an EXPIRED grant either.
    ok, _ = await approval.revoke_grant("99999", "OWNER", "bot1", "10001", "30007")
    assert ok is False
    assert (await _grant_row(db, "30007"))["status"] == "EXPIRED"


async def test_concurrent_rate_limit_check_is_atomic(system):
    """10 concurrent reservations against limit 3 must yield exactly 3 successes."""
    _, approval, _, _, _ = system
    results = await asyncio.gather(
        *(approval.check_and_increment_rate_limit("rate:test:key", 3, window_seconds=3600) for _ in range(10))
    )
    assert sum(1 for r in results if r) == 3, f"rate limit allowed {sum(results)}/10 (limit 3)"


@pytest.mark.parametrize("status", ["ACTIVE", "SUSPENDED"])
async def test_grant_expiring_during_level_lookup_cannot_authorize(system, monkeypatch, status):
    from types import SimpleNamespace

    from approval import access_service

    db, _approval, access, level, _config = system
    await _insert_active_grant(db)
    now = int(time.time())
    await db.execute("UPDATE grants SET status = ?, expires_at = ?", (status, now + 1))
    level.level = 20
    lookup = level.get_group_member_level

    async def slow_lookup(**kwargs):
        nonlocal now
        result = await lookup(**kwargs)
        now += 1
        return result

    monkeypatch.setattr(access_service, "time", SimpleNamespace(time=lambda: now))
    monkeypatch.setattr(level, "get_group_member_level", slow_lookup)
    decision = await access.check_access("bot1", "10001", "30005")
    assert not decision.allowed and decision.reason == "GRANT_EXPIRED"
    assert (await db.fetchone("SELECT status FROM grants"))[0] == "EXPIRED"
