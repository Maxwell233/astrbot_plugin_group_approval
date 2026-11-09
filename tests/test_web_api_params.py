"""Strict parameter-validation tests for the WebUI REST API backend.

Design constraints from the fix brief:

* real SQLite (``temp_db`` fixture) + the real ``WebApiHandler`` / service stack,
* the only fake is the NapCat transport (``_FakeNapCat`` below),
* mutations must require an attested digit-only ``bot_self_id`` and must never
  write ``"default"`` anywhere,
* bools/ints/enums/ids/notes have strict, bounded validation,
* audit export returns content + filename and rejects unknown formats.
"""

from __future__ import annotations

import csv
import io
import json
import re
from typing import Any

import pytest

from approval.approval_service import ApprovalService
from approval.audit_service import AuditService
from approval.level_service import LevelService
from approval.notification_service import NotificationService
from approval.web_api import (
    MAX_DURATION_DAYS,
    MAX_MIN_LEVEL,
    MAX_NOTE_LENGTH,
    MISSING_BOT_IDENTITY_MESSAGE,
    WebApiHandler,
)

BOT = "10001"
OTHER_BOT = "20002"
GROUP = "10001"
USER = "20001"

_AUDIT_EXPORT_HEADER = [
    "id",
    "timestamp",
    "actor_id",
    "actor_type",
    "bot_self_id",
    "group_id",
    "target_user_id",
    "action",
    "old_state",
    "new_state",
    "request_id",
    "details_json",
]


class _FakeNapCat:
    """Minimal fake transport: only network I/O is faked."""

    def __init__(self, level: str = "15") -> None:
        self.level = level
        self.bot_ids: list[str] = [BOT, OTHER_BOT]
        self.sent_private: list[tuple[str, str]] = []
        self.sent_group: list[tuple[str, str]] = []

    async def get_group_member_info(
        self,
        group_id: str | int,
        user_id: str | int,
        bot_self_id: str | None = None,
        no_cache: bool = True,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return {
            "status": "ok",
            "retcode": 0,
            "data": {
                "group_id": int(group_id),
                "user_id": int(user_id),
                "level": self.level,
                "role": "member",
            },
        }

    async def send_private_msg(
        self, user_id: str, message: str, bot_self_id: str | None = None, **kwargs: Any
    ) -> tuple[bool, dict[str, Any] | None, str, int]:
        self.sent_private.append((str(user_id), message))
        return True, {"message_id": 1}, "", 0

    async def send_group_msg(
        self, group_id: str, message: str, bot_self_id: str | None = None, **kwargs: Any
    ) -> tuple[bool, dict[str, Any] | None, str, int]:
        self.sent_group.append((str(group_id), message))
        return True, {"message_id": 2}, "", 0

    async def list_bots(self) -> list[str]:
        return list(self.bot_ids)


@pytest.fixture
def web_system(temp_db, test_config):
    """Real DB + real services; fake network transport only."""
    fake = _FakeNapCat()
    audit = AuditService(temp_db)
    notif = NotificationService(temp_db, fake)
    level = LevelService(fake)
    approval = ApprovalService(temp_db, level, audit, notif, test_config)
    web = WebApiHandler(temp_db, approval, audit, test_config, client=fake)
    return web, approval, audit, temp_db, test_config, fake


async def _count(db, table: str, where: str = "", params: tuple[Any, ...] = ()) -> int:
    row = await db.fetchone(f"SELECT COUNT(*) FROM {table} {where}".strip(), params)
    assert row is not None
    return int(row[0])


async def _pending_app(approval: ApprovalService, user: str = USER, bot: str = BOT):
    ok, msg, app = await approval.apply_for_access(bot, GROUP, user, "需要 Bot 权限")
    assert ok is True, msg
    assert app is not None
    return app


async def _assert_no_placeholder_rows(db) -> None:
    for table in ("grants", "group_policies", "blacklist"):
        assert await _count(db, table, "WHERE bot_self_id = 'default'") == 0, table


# ---------------------------------------------------------------------------
# 1. Attested bot identity required for every mutation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_blank_or_placeholder_bot_id_rejected_for_all_mutations(web_system):
    web, approval, _audit, db, _config, _fake = web_system
    app = await _pending_app(approval)

    cases = [
        ("grant", web.manual_grant, {"group_id": GROUP, "user_id": USER}),
        ("revoke", web.revoke_grant, {"group_id": GROUP, "user_id": USER}),
        ("ban", web.ban_user, {"group_id": GROUP, "user_id": USER}),
        ("unban", web.unban_user, {"group_id": GROUP, "user_id": USER}),
        ("update_group", web.update_group_policy, {"group_id": GROUP}),
        ("approve", web.approve_application, {"application_id": app.id}),
        ("reject", web.reject_application, {"application_id": app.id}),
    ]
    variants: list[Any] = [None, "", "   ", "default", "Default", "DEFAULT"]
    before = {
        "grants": await _count(db, "grants"),
        "blacklist": await _count(db, "blacklist"),
        "applications": await _count(db, "applications"),
        "policies": await _count(db, "group_policies"),
    }

    for label, method, base in cases:
        for variant in variants:
            payload = dict(base)
            if variant is not None:
                payload["bot_self_id"] = variant
            res = await method(payload, actor_id="99999", actor_role="OWNER")
            assert res["status"] == "error", (label, variant, res)
            assert res["retcode"] == 400, (label, variant, res)
            assert res["message"] == MISSING_BOT_IDENTITY_MESSAGE, (label, variant, res)

    # Nothing was written, the application is still pending.
    assert await _count(db, "grants") == before["grants"] == 0
    assert await _count(db, "blacklist") == before["blacklist"] == 0
    assert await _count(db, "applications") == before["applications"] == 1
    assert await _count(db, "group_policies") == before["policies"]
    row = await db.fetchone("SELECT status FROM applications WHERE id = ?", (app.id,))
    assert row["status"] == "PENDING"
    await _assert_no_placeholder_rows(db)


@pytest.mark.asyncio
async def test_non_numeric_bot_id_rejected(web_system):
    web, _approval, _audit, db, _config, _fake = web_system
    for bad_bot in ("abc", "bot1", "12a", "1 0001"):
        res = await web.manual_grant(
            {"bot_self_id": bad_bot, "group_id": GROUP, "user_id": USER},
            actor_id="99999",
            actor_role="OWNER",
        )
        assert res["status"] == "error", bad_bot
        assert res["retcode"] == 400
        assert "纯数字" in res["message"]
    assert await _count(db, "grants") == 0


# ---------------------------------------------------------------------------
# 2. QQ / group id syntax rules
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_numeric_user_and_group_ids_rejected(web_system):
    web, _approval, _audit, db, _config, _fake = web_system
    invalid_payloads = [
        (web.manual_grant, {"bot_self_id": BOT, "group_id": "abc", "user_id": USER}),
        (web.manual_grant, {"bot_self_id": BOT, "group_id": "10a01", "user_id": USER}),
        (web.manual_grant, {"bot_self_id": BOT, "group_id": GROUP, "user_id": "20x01"}),
        (web.manual_grant, {"bot_self_id": BOT, "group_id": "ALL", "user_id": USER}),
        (web.revoke_grant, {"bot_self_id": BOT, "group_id": "ALL", "user_id": USER}),
        (web.update_group_policy, {"bot_self_id": BOT, "group_id": "GROUP"}),
        (web.ban_user, {"bot_self_id": BOT, "group_id": "abc", "user_id": USER}),
    ]
    for method, payload in invalid_payloads:
        res = await method(payload, actor_id="99999", actor_role="OWNER")
        assert res["status"] == "error", payload
        assert res["retcode"] == 400
    assert await _count(db, "grants") == 0
    assert await _count(db, "blacklist") == 0


@pytest.mark.asyncio
async def test_global_blacklist_all_is_only_accepted_for_ban_and_unban(web_system):
    web, _approval, _audit, db, _config, _fake = web_system
    ban = await web.ban_user(
        {"bot_self_id": BOT, "group_id": "ALL", "user_id": USER, "reason": "违规"},
        actor_id="99999",
        actor_role="OWNER",
    )
    assert ban["status"] == "ok", ban
    row = await db.fetchone("SELECT bot_self_id, group_id FROM blacklist WHERE user_id = ?", (USER,))
    assert (row["bot_self_id"], row["group_id"]) == (BOT, "ALL")

    unban = await web.unban_user(
        {"bot_self_id": BOT, "group_id": "all", "user_id": USER},
        actor_id="99999",
        actor_role="OWNER",
    )
    assert unban["status"] == "ok", unban
    assert await _count(db, "blacklist") == 0


# ---------------------------------------------------------------------------
# 3. Valid writes persist the real bot identity (never "default")
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manual_grant_persists_verified_bot_id(web_system):
    web, _approval, _audit, db, _config, _fake = web_system
    res = await web.manual_grant(
        {"bot_self_id": BOT, "group_id": GROUP, "user_id": USER, "duration_days": 7, "note": "ok"},
        actor_id="99999",
        actor_role="OWNER",
    )
    assert res["status"] == "ok", res
    row = await db.fetchone("SELECT bot_self_id, expires_at FROM grants WHERE user_id = ?", (USER,))
    assert row["bot_self_id"] == BOT
    assert row["expires_at"] > 0
    await _assert_no_placeholder_rows(db)


@pytest.mark.asyncio
async def test_ban_persists_verified_bot_id_and_blocks_that_bot_only(web_system):
    web, approval, _audit, db, _config, _fake = web_system
    res = await web.ban_user(
        {"bot_self_id": BOT, "group_id": GROUP, "user_id": USER, "reason": "spam", "duration_days": 2},
        actor_id="99999",
        actor_role="OWNER",
    )
    assert res["status"] == "ok", res
    row = await db.fetchone("SELECT bot_self_id FROM blacklist WHERE user_id = ?", (USER,))
    assert row["bot_self_id"] == BOT
    assert (await approval.is_blacklisted(BOT, GROUP, USER))[0] is True
    assert (await approval.is_blacklisted(OTHER_BOT, GROUP, USER))[0] is False


@pytest.mark.asyncio
async def test_update_group_policy_persists_verified_bot_id(web_system):
    web, approval, _audit, db, _config, _fake = web_system
    res = await web.update_group_policy(
        {"bot_self_id": BOT, "group_id": GROUP, "enabled": True, "min_level": 20}
    )
    assert res["status"] == "ok", res
    row = await db.fetchone(
        "SELECT bot_self_id, min_level FROM group_policies WHERE group_id = ?", (GROUP,)
    )
    assert row["bot_self_id"] == BOT
    assert row["min_level"] == 20
    policy = await approval.get_group_policy(BOT, GROUP)
    assert policy.min_level == 20
    await _assert_no_placeholder_rows(db)


# ---------------------------------------------------------------------------
# 4. Strict booleans
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strict_bool_accepts_only_bool_zero_one_true_false(web_system):
    web, _approval, _audit, db, _config, _fake = web_system
    accepted = [
        (True, True),
        ("true", True),
        ("TRUE", True),
        ("1", True),
        (1, True),
        (False, False),
        ("false", False),
        ("0", False),
        (0, False),
    ]
    for raw, expected in accepted:
        res = await web.update_group_policy(
            {"bot_self_id": BOT, "group_id": GROUP, "enabled": raw}
        )
        assert res["status"] == "ok", (raw, res)
        row = await db.fetchone(
            "SELECT enabled FROM group_policies WHERE bot_self_id = ? AND group_id = ?",
            (BOT, GROUP),
        )
        assert bool(row["enabled"]) is expected, raw


@pytest.mark.asyncio
async def test_strict_bool_rejects_other_strings_and_values(web_system):
    web, _approval, _audit, db, _config, _fake = web_system
    for bad in ("no", "yes", "on", "off", "abc", "", None, 2, -1, 1.0):
        res = await web.update_group_policy(
            {"bot_self_id": BOT, "group_id": GROUP, "enabled": bad}
        )
        assert res["status"] == "error", (bad, res)
        assert res["retcode"] == 400
        assert "布尔值" in res["message"]
    # No policy row was written for the rejected calls.
    assert await _count(db, "group_policies") == 0


@pytest.mark.asyncio
async def test_strict_bool_applies_to_all_boolean_fields_and_level_exempt(web_system):
    web, _approval, _audit, db, _config, _fake = web_system
    for field in ("approval_required", "owner_bypass", "group_admin_bypass"):
        res = await web.update_group_policy(
            {"bot_self_id": BOT, "group_id": GROUP, field: "no"}
        )
        assert res["status"] == "error", (field, res)
    for bad in ("no", "yes", "abc", 2):
        res = await web.manual_grant(
            {
                "bot_self_id": BOT,
                "group_id": GROUP,
                "user_id": USER,
                "is_level_exempt": bad,
            },
            actor_id="99999",
            actor_role="OWNER",
        )
        assert res["status"] == "error", (bad, res)
        assert res["retcode"] == 400
    assert await _count(db, "grants") == 0


@pytest.mark.asyncio
async def test_level_exempt_still_requires_owner_actor_role(web_system):
    """The API must not widen actor_role for the owner-only exemption."""
    web, _approval, _audit, db, _config, _fake = web_system
    res = await web.manual_grant(
        {"bot_self_id": BOT, "group_id": GROUP, "user_id": USER, "is_level_exempt": True},
        actor_id="88888",
        actor_role="REVIEWER",
    )
    assert res["status"] == "error", res
    assert "仅机器人主人" in res["message"]
    assert await _count(db, "grants") == 0


# ---------------------------------------------------------------------------
# 5. Integer bounds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_int_bounds_rejected_for_policy_fields(web_system):
    web, _approval, _audit, db, _config, _fake = web_system
    cases = [
        ("min_level", -1),
        ("min_level", MAX_MIN_LEVEL + 1),
        ("min_level", "abc"),
        ("min_level", None),
        ("grant_duration_days", -1),
        ("grant_duration_days", MAX_DURATION_DAYS + 1),
        ("grant_duration_days", 1.5),
        ("recheck_interval_seconds", 9),
        ("recheck_interval_seconds", 86401),
    ]
    for field, value in cases:
        res = await web.update_group_policy(
            {"bot_self_id": BOT, "group_id": GROUP, field: value}
        )
        assert res["status"] == "error", (field, value, res)
        assert res["retcode"] == 400
    assert await _count(db, "group_policies") == 0

    # Boundary values are accepted.
    ok = await web.update_group_policy(
        {
            "bot_self_id": BOT,
            "group_id": GROUP,
            "min_level": 0,
            "grant_duration_days": 0,
            "recheck_interval_seconds": 10,
        }
    )
    assert ok["status"] == "ok", ok
    ok2 = await web.update_group_policy(
        {
            "bot_self_id": BOT,
            "group_id": GROUP,
            "min_level": MAX_MIN_LEVEL,
            "grant_duration_days": MAX_DURATION_DAYS,
            "recheck_interval_seconds": 86400,
        }
    )
    assert ok2["status"] == "ok", ok2


@pytest.mark.asyncio
async def test_int_bounds_rejected_for_grant_and_ban_durations(web_system):
    web, _approval, _audit, db, _config, _fake = web_system
    for bad in (-1, MAX_DURATION_DAYS + 1, None, "abc", 1.5):
        grant = await web.manual_grant(
            {"bot_self_id": BOT, "group_id": GROUP, "user_id": USER, "duration_days": bad},
            actor_id="99999",
            actor_role="OWNER",
        )
        assert grant["status"] == "error", ("grant", bad, grant)
        ban = await web.ban_user(
            {"bot_self_id": BOT, "group_id": GROUP, "user_id": USER, "duration_days": bad},
            actor_id="99999",
            actor_role="OWNER",
        )
        assert ban["status"] == "error", ("ban", bad, ban)
    assert await _count(db, "grants") == 0
    assert await _count(db, "blacklist") == 0


# ---------------------------------------------------------------------------
# 6. Enumerations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enum_validation(web_system):
    web, _approval, _audit, _db, _config, _fake = web_system
    bad_policy = await web.update_group_policy(
        {"bot_self_id": BOT, "group_id": GROUP, "restore_policy": "bogus"}
    )
    assert bad_policy["status"] == "error"

    good_policy = await web.update_group_policy(
        {"bot_self_id": BOT, "group_id": GROUP, "restore_policy": "MANUAL"}
    )
    assert good_policy["status"] == "ok", good_policy
    assert good_policy["data"]["policy"]["restore_policy"] == "manual"

    assert (await web.list_applications(status="HACK"))["status"] == "error"
    assert (await web.list_applications(status="pending"))["status"] == "ok"
    assert (await web.list_users(status="PENDING"))["status"] == "error"
    assert (await web.list_users(status="active"))["status"] == "ok"
    assert (await web.export_audit_logs(format_type="xml"))["status"] == "error"


# ---------------------------------------------------------------------------
# 7. Note / reason length caps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_note_and_reason_length_capped(web_system):
    web, approval, _audit, db, _config, _fake = web_system
    app = await _pending_app(approval)
    long_note = "x" * (MAX_NOTE_LENGTH + 1)
    exact_note = "x" * MAX_NOTE_LENGTH

    cases = [
        (web.manual_grant, {"bot_self_id": BOT, "group_id": GROUP, "user_id": USER, "note": long_note}),
        (web.ban_user, {"bot_self_id": BOT, "group_id": GROUP, "user_id": USER, "reason": long_note}),
        (web.revoke_grant, {"bot_self_id": BOT, "group_id": GROUP, "user_id": USER, "reason": long_note}),
        (
            web.approve_application,
            {"application_id": app.id, "bot_self_id": BOT, "note": long_note},
        ),
        (
            web.reject_application,
            {"application_id": app.id, "bot_self_id": BOT, "reason": long_note},
        ),
    ]
    for method, payload in cases:
        res = await method(payload, actor_id="99999", actor_role="OWNER")
        assert res["status"] == "error", (method.__name__, res)
        assert res["retcode"] == 400
        assert "长度不能超过" in res["message"]

    ok_grant = await web.manual_grant(
        {"bot_self_id": BOT, "group_id": GROUP, "user_id": USER, "note": exact_note},
        actor_id="99999",
        actor_role="OWNER",
    )
    assert ok_grant["status"] == "ok", ok_grant
    assert await _count(db, "grants") == 1


# ---------------------------------------------------------------------------
# 8. Approve / reject bot-attestation matching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_and_reject_require_matching_application_bot_id(web_system):
    web, approval, _audit, db, _config, _fake = web_system
    app = await _pending_app(approval)

    mismatch = await web.approve_application(
        {"application_id": app.id, "bot_self_id": OTHER_BOT}, actor_id="99999", actor_role="OWNER"
    )
    assert mismatch["status"] == "error"
    assert "不一致" in mismatch["message"]
    row = await db.fetchone("SELECT status FROM applications WHERE id = ?", (app.id,))
    assert row["status"] == "PENDING"

    approved = await web.approve_application(
        {"application_id": app.id, "bot_self_id": BOT, "note": "ok"},
        actor_id="99999",
        actor_role="OWNER",
    )
    assert approved["status"] == "ok", approved
    grant = await db.fetchone("SELECT bot_self_id, status FROM grants WHERE user_id = ?", (USER,))
    assert (grant["bot_self_id"], grant["status"]) == (BOT, "ACTIVE")

    app2 = await _pending_app(approval, user="20002")
    rejected = await web.reject_application(
        {"application_id": app2.id, "bot_self_id": BOT, "reason": "不符合"},
        actor_id="99999",
        actor_role="OWNER",
    )
    assert rejected["status"] == "ok", rejected
    row2 = await db.fetchone("SELECT status FROM applications WHERE id = ?", (app2.id,))
    assert row2["status"] == "REJECTED"


# ---------------------------------------------------------------------------
# 9. list_bots: verified ids only, never "default"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_bots_db_derived_never_returns_default(web_system):
    _, approval, audit, db, config, _fake = web_system
    web = WebApiHandler(db, approval, audit, config)
    await db.execute(
        "INSERT INTO group_policies (bot_self_id, group_id, enabled, min_level, created_at, updated_at) "
        "VALUES ('10002', '10001', 1, 10, 1, 1)"
    )
    await db.execute(
        "INSERT INTO grants (id, bot_self_id, group_id, user_id, approved_at) "
        "VALUES ('G1', '10001', '10001', '30001', 1)"
    )
    await db.execute(
        "INSERT INTO blacklist (bot_self_id, group_id, user_id, created_at) "
        "VALUES ('10003', '10001', '30002', 1)"
    )
    # Legacy placeholder rows must never leak into the bot list.
    await db.execute(
        "INSERT INTO group_policies (bot_self_id, group_id, enabled, min_level, created_at, updated_at) "
        "VALUES ('default', '10001', 1, 10, 1, 1)"
    )
    await db.execute(
        "INSERT INTO grants (id, bot_self_id, group_id, user_id, approved_at) "
        "VALUES ('G2', 'default', '10001', '30003', 1)"
    )
    await db.commit()

    res = await web.list_bots()
    assert res["status"] == "ok"
    assert res["data"]["bots"] == ["10001", "10002", "10003"]


@pytest.mark.asyncio
async def test_list_bots_prefers_verified_client_discovery(web_system):
    web, approval, audit, db, config, fake = web_system
    await db.execute(
        "INSERT INTO group_policies (bot_self_id, group_id, enabled, min_level, created_at, updated_at) "
        "VALUES ('10009', '10001', 1, 10, 1, 1)"
    )
    await db.commit()

    fake.bot_ids = ["20002", "10001", "default", "", "   "]
    web_with_client = WebApiHandler(db, approval, audit, config, client=fake)
    res = await web_with_client.list_bots()
    assert res["data"]["bots"] == ["10001", "20002"]
    assert "10009" not in res["data"]["bots"]  # verified discovery wins, no DB union


@pytest.mark.asyncio
async def test_list_bots_falls_back_when_client_discovery_fails(web_system):
    web, approval, audit, db, config, fake = web_system
    await db.execute(
        "INSERT INTO group_policies (bot_self_id, group_id, enabled, min_level, created_at, updated_at) "
        "VALUES ('10001', '10001', 1, 10, 1, 1)"
    )
    await db.commit()

    async def _boom() -> list[str]:
        raise ConnectionError("napcat down")

    fake.list_bots = _boom  # type: ignore[method-assign]
    web_with_client = WebApiHandler(db, approval, audit, config, client=fake)
    res = await web_with_client.list_bots()
    assert res["status"] == "ok"
    assert res["data"]["bots"] == ["10001"]


# ---------------------------------------------------------------------------
# 10. List filters are validated, bot filters never accept "default"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_filters_validation(web_system):
    web, approval, _audit, db, _config, _fake = web_system
    await web.manual_grant(
        {"bot_self_id": BOT, "group_id": GROUP, "user_id": USER},
        actor_id="99999",
        actor_role="OWNER",
    )
    await web.manual_grant(
        {"bot_self_id": OTHER_BOT, "group_id": GROUP, "user_id": "20009"},
        actor_id="99999",
        actor_role="OWNER",
    )

    all_users = await web.list_users()
    assert all_users["data"]["count"] == 2
    filtered = await web.list_users(bot_self_id=BOT)
    assert filtered["data"]["count"] == 1
    assert filtered["data"]["grants"][0]["bot_self_id"] == BOT

    assert (await web.list_users(bot_self_id=""))["data"]["count"] == 2
    assert (await web.list_users(bot_self_id="default"))["status"] == "error"
    assert (await web.list_users(group_id="abc"))["status"] == "error"
    assert (await web.list_users(user_id="x"))["status"] == "error"
    assert (await web.list_applications(status="BOGUS"))["status"] == "error"
    assert (await web.list_groups(bot_self_id="default"))["status"] == "error"
    assert (await web.list_audit_logs(group_id="abc"))["status"] == "error"
    assert (await web.list_audit_logs(bot_self_id="default"))["status"] == "error"


# ---------------------------------------------------------------------------
# 11. Audit export shape: validated format, content + filename
# ---------------------------------------------------------------------------


async def _seed_audit_logs(audit: AuditService) -> None:
    await audit.log(
        actor_id="99999",
        actor_type="OWNER",
        bot_self_id=BOT,
        group_id=GROUP,
        target_user_id=USER,
        action="MANUAL_GRANT",
        details={"comment": 'a,b"c', "multi": "line1\nline2"},
    )
    await audit.log(
        actor_id="88888",
        actor_type="REVIEWER",
        bot_self_id=BOT,
        group_id=GROUP,
        target_user_id=USER,
        action="APPLICATION_REJECTED",
        details={"reason": "spam"},
    )


@pytest.mark.asyncio
async def test_export_json_returns_content_and_filename(web_system):
    web, _approval, audit, _db, _config, _fake = web_system
    await _seed_audit_logs(audit)

    res = await web.export_audit_logs("json")
    assert res["status"] == "ok"
    assert res["retcode"] == 0
    assert res["format"] == "json"
    assert re.fullmatch(r"audit_logs_\d+\.json", res["filename"]), res["filename"]
    payload = json.loads(res["data"])
    assert isinstance(payload, list)
    assert {entry["action"] for entry in payload} == {"MANUAL_GRANT", "APPLICATION_REJECTED"}
    detail = next(e for e in payload if e["action"] == "MANUAL_GRANT")
    assert json.loads(detail["details_json"])["comment"] == 'a,b"c'


@pytest.mark.asyncio
async def test_export_csv_is_well_formed_with_commas_and_quotes(web_system):
    web, _approval, audit, _db, _config, _fake = web_system
    await _seed_audit_logs(audit)

    res = await web.export_audit_logs("csv")
    assert res["status"] == "ok"
    assert res["format"] == "csv"
    assert re.fullmatch(r"audit_logs_\d+\.csv", res["filename"]), res["filename"]

    rows = list(csv.reader(io.StringIO(res["data"])))
    assert rows[0] == _AUDIT_EXPORT_HEADER
    assert len(rows) == 3  # header + 2 entries
    for row in rows[1:]:
        assert len(row) == len(_AUDIT_EXPORT_HEADER)
        # csv quoting round-trips the embedded comma/quote/newline JSON.
        details = json.loads(row[-1])
        assert isinstance(details, dict)
    grant_row = next(r for r in rows[1:] if r[7] == "MANUAL_GRANT")
    assert json.loads(grant_row[-1])["comment"] == 'a,b"c'


@pytest.mark.asyncio
async def test_export_default_format_and_filter_validation(web_system):
    web, _approval, _audit, _db, _config, _fake = web_system
    for default in (None, "", "JSON", " Json "):
        res = await web.export_audit_logs(default)
        assert res["status"] == "ok", default
        assert res["format"] == "json"

    for bad in ("xml", "yaml", "exe"):
        res = await web.export_audit_logs(bad)
        assert res["status"] == "error"
        assert res["retcode"] == 400

    assert (await web.export_audit_logs("json", bot_self_id="default"))["status"] == "error"
    assert (await web.export_audit_logs("json", group_id="abc"))["status"] == "error"
    assert (await web.export_audit_logs("json", limit=0))["status"] == "error"
    assert (await web.export_audit_logs("json", limit=5001))["status"] == "error"
    assert (await web.export_audit_logs("json", start_time="x"))["status"] == "error"


@pytest.mark.parametrize("method", [
    "manual_grant", "revoke_grant", "ban_user", "unban_user", "update_group_policy",
    "approve_application", "reject_application",
])
async def test_every_mutation_rejects_numeric_bots_without_live_verification(web_system, method):
    _, approval, audit, db, config, fake = web_system
    app = await _pending_app(approval)
    fake.bot_ids = [OTHER_BOT]
    web = WebApiHandler(db, approval, audit, config, client=fake)
    response = await getattr(web, method)(
        {"bot_self_id": BOT, "group_id": GROUP, "user_id": USER, "application_id": app.id},
        actor_id="99999", actor_role="OWNER",
    )
    assert response["retcode"] == 403, response
    assert (await db.fetchone("SELECT status FROM applications WHERE id = ?", (app.id,)))[0] == "PENDING"
    assert await _count(db, "grants") == await _count(db, "blacklist") == 0


async def test_dashboard_counts_and_recent_applications_are_scoped(web_system):
    web, approval, _audit, _db, _config, _fake = web_system
    first = await _pending_app(approval, bot=BOT)
    await _pending_app(approval, bot=OTHER_BOT)
    result = (await web.get_dashboard_stats(BOT))["data"]
    assert result["stats"]["pending_applications"] == 1
    assert [row["id"] for row in result["recent_applications"]] == [first.id]
    assert result["recent_applications"][0]["bot_self_id"] == BOT
    assert (await web.get_dashboard_stats())["data"]["stats"]["pending_applications"] == 2
