"""REST API handlers for AstrBot WebUI management views.

Validation contract (v0.1.1 security hardening):

* Dashboard mutations may never operate on a fabricated bot identity.  Every
  mutating handler requires an explicitly supplied, digit-only ``bot_self_id``;
  missing / blank / ``"default"`` values are rejected with a 400-style error and
  no database write is attempted.
* QQ / user / group ids are digit-only.  ``"ALL"`` is accepted only for the two
  blacklist endpoints that explicitly support a global ban (``ban``/``unban``).
* Integers are real integers with explicit bounds; booleans use a strict parser
  (real ``bool``, ``0``/``1``, ``"true"``/``"false"`` only --- never
  ``bool("false")``) and all other strings are rejected.
* Enumerations (application status, grant status, restore policy, export format)
  are validated against closed sets.
* Notes / reasons are length-capped to :data:`MAX_NOTE_LENGTH` characters.
* Every write path still receives ``actor_id`` / ``actor_role`` from the route
  layer (``main.py``) and passes them straight to the service layer; this module
  never derives a role or widens privileges.
"""

from __future__ import annotations

import inspect
import logging
import re
import time
from typing import Any

from .approval_service import ApprovalService
from .audit_service import AuditService
from .config import PluginConfig
from .database import Database

logger = logging.getLogger("astrbot_plugin_group_approval.web_api")

#: Placeholder that older releases wrote as a bot identity.  It is never a valid id.
BOT_IDENTITY_PLACEHOLDER = "default"

#: Exact error message required for missing / blank / placeholder bot identity.
MISSING_BOT_IDENTITY_MESSAGE = "未指定已验证的 Bot 身份，已拒绝操作"

MAX_NOTE_LENGTH = 500
MAX_QQ_ID_LENGTH = 20
MAX_APPLICATION_ID_LENGTH = 64
MAX_AUDIT_ACTION_LENGTH = 64
MAX_PAGE_LIMIT = 1000
MAX_EXPORT_LIMIT = 5000
MAX_OFFSET = 1_000_000
MAX_DURATION_DAYS = 36500
MAX_MIN_LEVEL = 1000
MAX_RECHECK_INTERVAL_SECONDS = 86400
MIN_RECHECK_INTERVAL_SECONDS = 10

APPLICATION_STATUSES = frozenset({"PENDING", "APPROVED", "REJECTED", "CANCELLED", "EXPIRED"})
GRANT_STATUSES = frozenset({"ACTIVE", "SUSPENDED", "REVOKED", "EXPIRED"})
RESTORE_POLICIES = frozenset({"automatic", "manual"})
EXPORT_FORMATS = frozenset({"json", "csv"})

_DIGITS_RE = re.compile(r"^\d+$")
_INT_RE = re.compile(r"^-?\d+$")
_APPLICATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class ParamError(ValueError):
    """Raised when a request parameter fails strict validation."""


def parse_bool(val: Any, default: bool = False) -> bool:
    """Legacy lenient boolean coercion.

    Kept for backward compatibility with callers that imported it historically
    (``main.py`` still imports it for the authorized-mutation shortcut).
    **Web API handlers do not use this function**; they use
    :func:`_parse_strict_bool`, which rejects unknown strings instead of
    silently coercing them.
    """
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return val != 0
    s = str(val).strip().lower()
    if s in ("true", "1", "yes", "on", "t"):
        return True
    if s in ("false", "0", "no", "off", "f"):
        return False
    return default


def _parse_strict_bool(value: Any, field: str) -> bool:
    """Strict boolean parser: bool, 0/1, "true"/"false" (case-insensitive) only."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        if value in (0, 1):
            return bool(value)
        raise ParamError(f"{field} 必须为布尔值（true/false/0/1）")
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("true", "1"):
            return True
        if s in ("false", "0"):
            return False
    raise ParamError(f"{field} 必须为布尔值（true/false/0/1）")


def _parse_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    """Parse a real integer (int or digit string) and enforce inclusive bounds."""
    if isinstance(value, bool):
        raise ParamError(f"{field} 必须为整数")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and _INT_RE.match(value.strip()):
        parsed = int(value.strip())
    else:
        raise ParamError(f"{field} 必须为整数")
    if parsed < minimum or parsed > maximum:
        raise ParamError(f"{field} 必须在 {minimum} 到 {maximum} 之间")
    return parsed


def _parse_optional_int(
    value: Any, field: str, minimum: int, maximum: int, default: int
) -> int:
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    return _parse_int(value, field, minimum, maximum)


def _parse_present_int(
    payload: dict[str, Any], key: str, field: str, minimum: int, maximum: int
) -> int | None:
    """Parse an integer that is present in ``payload`` (``None`` is invalid)."""
    if key not in payload:
        return None
    value = payload[key]
    if value is None:
        raise ParamError(f"{field} 不能为空")
    return _parse_int(value, field, minimum, maximum)


def _parse_digits(value: Any, field: str) -> str:
    """Parse a required digit-only QQ id."""
    if value is None:
        raise ParamError(f"缺少 {field} 参数")
    s = str(value).strip()
    if not s:
        raise ParamError(f"缺少 {field} 参数")
    if not _DIGITS_RE.match(s):
        raise ParamError(f"{field} 必须为纯数字 QQ 号")
    if len(s) > MAX_QQ_ID_LENGTH:
        raise ParamError(f"{field} 长度不能超过 {MAX_QQ_ID_LENGTH} 位")
    return s


def _parse_optional_digits(value: Any, field: str) -> str | None:
    """Parse an optional digit-only QQ id (blank/None means 'no filter')."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if not _DIGITS_RE.match(s):
        raise ParamError(f"{field} 必须为纯数字 QQ 号")
    if len(s) > MAX_QQ_ID_LENGTH:
        raise ParamError(f"{field} 长度不能超过 {MAX_QQ_ID_LENGTH} 位")
    return s


def _parse_group_id(value: Any, field: str = "group_id", *, allow_all: bool = False) -> str:
    """Parse a group id.  ``ALL`` is only allowed for global blacklist operations."""
    if value is None:
        raise ParamError(f"缺少 {field} 参数")
    s = str(value).strip()
    if not s:
        raise ParamError(f"缺少 {field} 参数")
    if allow_all and s.upper() == "ALL":
        return "ALL"
    if not _DIGITS_RE.match(s):
        raise ParamError(f"{field} 必须为纯数字 QQ 群号")
    if len(s) > MAX_QQ_ID_LENGTH:
        raise ParamError(f"{field} 长度不能超过 {MAX_QQ_ID_LENGTH} 位")
    return s


def _parse_bot_self_id(payload: dict[str, Any]) -> str:
    """Return the explicitly attested bot identity for a mutating request."""
    raw = payload.get("bot_self_id") if isinstance(payload, dict) else None
    if raw is None:
        raise ParamError(MISSING_BOT_IDENTITY_MESSAGE)
    s = str(raw).strip()
    if not s or s.lower() == BOT_IDENTITY_PLACEHOLDER:
        raise ParamError(MISSING_BOT_IDENTITY_MESSAGE)
    if not _DIGITS_RE.match(s):
        raise ParamError("bot_self_id 必须为已验证的纯数字 Bot QQ 号")
    if len(s) > MAX_QQ_ID_LENGTH:
        raise ParamError(f"bot_self_id 长度不能超过 {MAX_QQ_ID_LENGTH} 位")
    return s


def _parse_bot_filter(value: Any) -> str | None:
    """Parse an optional ``bot_self_id`` list filter.

    Blank / missing means "no bot filter".  The legacy placeholder is never a
    valid id and is rejected instead of being silently treated as a filter.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s.lower() == BOT_IDENTITY_PLACEHOLDER:
        raise ParamError("bot_self_id 不能使用占位符 'default'，请留空表示全部 Bot")
    if not _DIGITS_RE.match(s):
        raise ParamError("bot_self_id 必须为纯数字 Bot QQ 号")
    if len(s) > MAX_QQ_ID_LENGTH:
        raise ParamError(f"bot_self_id 长度不能超过 {MAX_QQ_ID_LENGTH} 位")
    return s


def _parse_note(value: Any, field: str, default: str = "") -> str:
    """Parse an optional free-text note/reason with a hard length cap."""
    if value is None:
        return default
    s = str(value).strip()
    if len(s) > MAX_NOTE_LENGTH:
        raise ParamError(f"{field} 长度不能超过 {MAX_NOTE_LENGTH} 个字符")
    return s or default


def _parse_application_id(value: Any) -> str:
    if value is None:
        raise ParamError("缺少 application_id 参数")
    s = str(value).strip()
    if not s:
        raise ParamError("缺少 application_id 参数")
    if len(s) > MAX_APPLICATION_ID_LENGTH or not _APPLICATION_ID_RE.match(s):
        raise ParamError("application_id 格式非法")
    return s


def _parse_status(value: Any, field: str, allowed: frozenset[str]) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    upper = s.upper()
    if upper not in allowed:
        raise ParamError(f"{field} 非法，仅支持：{', '.join(sorted(allowed))}")
    return upper


def _parse_restore_policy(value: Any) -> str:
    s = str(value).strip().lower()
    if s not in RESTORE_POLICIES:
        raise ParamError("restore_policy 非法，仅支持：automatic, manual")
    return s


def _parse_export_format(value: Any) -> str:
    if value is None:
        return "json"
    s = str(value).strip().lower()
    if not s:
        return "json"
    if s not in EXPORT_FORMATS:
        raise ParamError("format 非法，仅支持：json, csv")
    return s


def _parse_audit_action(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if len(s) > MAX_AUDIT_ACTION_LENGTH:
        raise ParamError(f"action 长度不能超过 {MAX_AUDIT_ACTION_LENGTH} 个字符")
    return s


def _parse_limit_offset(limit: Any, offset: Any) -> tuple[int, int]:
    return (
        _parse_optional_int(limit, "limit", 1, MAX_PAGE_LIMIT, 50),
        _parse_optional_int(offset, "offset", 0, MAX_OFFSET, 0),
    )


def _clean_bot_ids(raw_ids: Any) -> set[str]:
    """Normalise an iterable of candidate bot ids, dropping blanks/placeholders."""
    cleaned: set[str] = set()
    if not isinstance(raw_ids, (list, tuple, set, frozenset)):
        return cleaned
    for raw in raw_ids:
        if raw is None:
            continue
        value = str(raw).strip()
        if not value or value.lower() == BOT_IDENTITY_PLACEHOLDER:
            continue
        cleaned.add(value)
    return cleaned


class WebApiHandler:
    """Provides REST endpoints consumed by the Plugin Pages dashboard."""

    def __init__(
        self,
        db: Database,
        approval_service: ApprovalService,
        audit_service: AuditService,
        config: PluginConfig,
        client: Any = None,
    ):
        self.db = db
        self.approval = approval_service
        self.audit = audit_service
        self.config = config
        #: Optional NapCatClient-like object used for verified bot discovery.
        #: ``main.py`` passes ``client=self.napcat``; when omitted the handler
        #: falls back to platform-manager / database derived ids.
        self.client = client

    # Helper response constructors
    def ok(self, data: Any = None) -> dict[str, Any]:
        return {"status": "ok", "retcode": 0, "data": data if data is not None else {}}

    def err(self, message: str, retcode: int = 400) -> dict[str, Any]:
        return {"status": "error", "retcode": retcode, "message": message}

    # ------------------------------------------------------------------
    # Verified bot discovery
    # ------------------------------------------------------------------
    async def _verified_bot_ids_from_client(self) -> set[str] | None:
        """Ask the NapCat client for verified ids.

        Returns ``None`` when the handler has no usable client (so callers fall
        back to platform-manager / database discovery), and a (possibly empty)
        set when the client answered.
        """
        client = self.client
        if client is None:
            return None
        list_bots = getattr(client, "list_bots", None)
        if not callable(list_bots):
            return None
        try:
            result = list_bots()
            if inspect.isawaitable(result):
                result = await result
        except Exception:  # noqa: BLE001 - discovery must never break the endpoint
            logger.exception("Verified bot discovery via NapCat client failed")
            return None
        return _clean_bot_ids(result)

    async def _bot_ids_from_database(self) -> set[str]:
        """Derive known bot ids from stored policy/grant/blacklist rows."""
        ids: set[str] = set()
        rows = await self.db.fetchall(
            """
            SELECT DISTINCT bot_self_id FROM group_policies
            WHERE TRIM(bot_self_id) != ''
            UNION
            SELECT DISTINCT bot_self_id FROM grants
            WHERE TRIM(bot_self_id) != ''
            UNION
            SELECT DISTINCT bot_self_id FROM blacklist
            WHERE TRIM(bot_self_id) != ''
            """
        )
        for row in rows:
            value = str(row["bot_self_id"] or "").strip()
            if value:
                ids.add(value)
        return ids

    async def list_bots(self, context: Any = None) -> dict[str, Any]:
        """Return verified/known bot identities; never fabricate a placeholder.

        Preference order (per the hardening brief):
        1. ``client.list_bots()`` when the handler was constructed with a
           NapCatClient-like object (verified via get_login_info / event
           attestation inside the client);
        2. otherwise database-derived ids from ``group_policies`` / ``grants`` /
           ``blacklist``.
        No fabricated or unverified placeholder id is ever returned.  The
        ``context`` argument is kept for backward compatibility with the route
        layer, but is not used as an identity source.
        """
        ids = await self._verified_bot_ids_from_client()
        if ids is None:
            ids = await self._bot_ids_from_database()
        ids = {str(i).strip() for i in ids if str(i).strip()}
        ids = {i for i in ids if i.lower() != BOT_IDENTITY_PLACEHOLDER}
        return self.ok({"bots": sorted(ids)})

    # ------------------------------------------------------------------
    # Read-only endpoints
    # ------------------------------------------------------------------
    async def get_dashboard_stats(self) -> dict[str, Any]:
        """Summary metrics for the dashboard view."""
        g_row = await self.db.fetchone("SELECT COUNT(*) FROM group_policies WHERE enabled = 1")
        p_row = await self.db.fetchone("SELECT COUNT(*) FROM applications WHERE status = 'PENDING'")
        a_row = await self.db.fetchone("SELECT COUNT(*) FROM grants WHERE status = 'ACTIVE'")
        s_row = await self.db.fetchone("SELECT COUNT(*) FROM grants WHERE status = 'SUSPENDED'")
        b_row = await self.db.fetchone("SELECT COUNT(*) FROM blacklist")

        recent_apps = await self.db.fetchall(
            """
            SELECT id, group_id, user_id, level_at_application, status, created_at, reviewed_at, reviewer_id
            FROM applications
            ORDER BY created_at DESC LIMIT 5
            """
        )

        apps_list = []
        for r in recent_apps:
            apps_list.append(
                {
                    "id": r["id"],
                    "group_id": r["group_id"],
                    "user_id": r["user_id"],
                    "level": r["level_at_application"],
                    "status": r["status"],
                    "created_at": r["created_at"],
                    "reviewed_at": r["reviewed_at"],
                    "reviewer_id": r["reviewer_id"],
                }
            )

        return self.ok(
            {
                "stats": {
                    "enabled_groups": g_row[0] if g_row else 0,
                    "pending_applications": p_row[0] if p_row else 0,
                    "active_grants": a_row[0] if a_row else 0,
                    "suspended_grants": s_row[0] if s_row else 0,
                    "blacklisted_users": b_row[0] if b_row else 0,
                },
                "recent_applications": apps_list,
                "plugin_info": {
                    "enabled": self.config.enabled,
                    "default_policy": self.config.default_policy,
                    "owners_count": len(self.config.owner_qq_ids),
                    "reviewers_count": len(self.config.reviewer_qq_ids),
                },
            }
        )

    async def list_applications(
        self,
        group_id: str | None = None,
        status: str | None = None,
        user_id: str | None = None,
        bot_self_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Fetch applications with validated filtering."""
        try:
            bot = _parse_bot_filter(bot_self_id)
            gid = _parse_optional_digits(group_id, "group_id")
            uid = _parse_optional_digits(user_id, "user_id")
            app_status = _parse_status(status, "status", APPLICATION_STATUSES)
            limit, offset = _parse_limit_offset(limit, offset)
        except ParamError as exc:
            return self.err(str(exc))

        clauses = []
        params: list[Any] = []
        if bot:
            clauses.append("bot_self_id = ?")
            params.append(bot)
        if gid:
            clauses.append("group_id = ?")
            params.append(gid)
        if app_status:
            clauses.append("status = ?")
            params.append(app_status)
        if uid:
            clauses.append("user_id = ?")
            params.append(uid)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
        SELECT id, bot_self_id, group_id, user_id, level_at_application, status,
               reason, created_at, expires_at, reviewed_at, reviewer_id, review_note
        FROM applications
        {where}
        ORDER BY created_at DESC
        LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])

        rows = await self.db.fetchall(sql, tuple(params))
        data = [dict(r) for r in rows]
        return self.ok({"applications": data, "count": len(data)})

    async def list_users(
        self,
        group_id: str | None = None,
        user_id: str | None = None,
        status: str | None = None,
        bot_self_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List user grants with validated filtering."""
        try:
            bot = _parse_bot_filter(bot_self_id)
            gid = _parse_optional_digits(group_id, "group_id")
            uid = _parse_optional_digits(user_id, "user_id")
            grant_status = _parse_status(status, "status", GRANT_STATUSES)
            limit, offset = _parse_limit_offset(limit, offset)
        except ParamError as exc:
            return self.err(str(exc))

        clauses = []
        params: list[Any] = []
        if bot:
            clauses.append("bot_self_id = ?")
            params.append(bot)
        if gid:
            clauses.append("group_id = ?")
            params.append(gid)
        if uid:
            clauses.append("user_id = ?")
            params.append(uid)
        if grant_status:
            clauses.append("status = ?")
            params.append(grant_status)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
        SELECT id, bot_self_id, group_id, user_id, status, source_application_id,
               approved_by, approved_at, expires_at, last_verified_level, last_verified_at,
               suspend_reason, revoked_by, revoked_at, grant_type, is_level_exempt
        FROM grants
        {where}
        ORDER BY approved_at DESC
        LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])

        rows = await self.db.fetchall(sql, tuple(params))
        data = [dict(r) for r in rows]
        return self.ok({"grants": data, "count": len(data)})

    async def list_groups(self, bot_self_id: str | None = None) -> dict[str, Any]:
        """List configured group policies with optional validated bot filter."""
        try:
            bot = _parse_bot_filter(bot_self_id)
        except ParamError as exc:
            return self.err(str(exc))
        if bot:
            rows = await self.db.fetchall(
                "SELECT * FROM group_policies WHERE bot_self_id = ? ORDER BY created_at DESC",
                (bot,),
            )
        else:
            rows = await self.db.fetchall("SELECT * FROM group_policies ORDER BY created_at DESC")
        return self.ok({"groups": [dict(r) for r in rows]})

    async def list_audit_logs(
        self,
        group_id: str | None = None,
        user_id: str | None = None,
        action: str | None = None,
        bot_self_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Fetch audit log entries with validated filtering."""
        try:
            bot = _parse_bot_filter(bot_self_id)
            gid = _parse_optional_digits(group_id, "group_id")
            uid = _parse_optional_digits(user_id, "user_id")
            act = _parse_audit_action(action)
            limit, offset = _parse_limit_offset(limit, offset)
        except ParamError as exc:
            return self.err(str(exc))

        logs = await self.audit.query(
            bot_self_id=bot,
            group_id=gid,
            target_user_id=uid,
            action=act,
            limit=limit,
            offset=offset,
        )
        return self.ok({"audit_logs": [e.to_dict() for e in logs]})

    # ------------------------------------------------------------------
    # Audit-log export
    # ------------------------------------------------------------------
    async def export_audit_logs(
        self,
        format_type: str = "json",
        bot_self_id: str | None = None,
        group_id: str | None = None,
        action: str | None = None,
        start_time: int | None = None,
        end_time: int | None = None,
        limit: int = MAX_EXPORT_LIMIT,
    ) -> dict[str, Any]:
        """Export audit logs as validated JSON/CSV with a download filename.

        Returns a complete API payload::

            {"status": "ok", "retcode": 0, "format": "json"|"csv",
             "filename": "audit_logs_<epoch>.<ext>", "data": "<content string>"}

        Invalid ``format`` / filters return a 400-style error payload instead of
        silently falling back to another format.
        """
        try:
            fmt = _parse_export_format(format_type)
            bot = _parse_bot_filter(bot_self_id)
            gid = _parse_optional_digits(group_id, "group_id")
            act = _parse_audit_action(action)
            export_limit = _parse_int(limit, "limit", 1, MAX_EXPORT_LIMIT)
            start = _parse_optional_int(start_time, "start_time", 0, 2**31 - 1, 0) if start_time else None
            end = _parse_optional_int(end_time, "end_time", 0, 2**31 - 1, 0) if end_time else None
        except ParamError as exc:
            return self.err(str(exc))

        kwargs: dict[str, Any] = {
            "bot_self_id": bot,
            "group_id": gid,
            "action": act,
            "start_time": start,
            "end_time": end,
            "limit": export_limit,
        }
        if fmt == "csv":
            content = await self.audit.export_csv(**kwargs)
        else:
            content = await self.audit.export_json(**kwargs)

        filename = f"audit_logs_{int(time.time())}.{fmt}"
        return {
            "status": "ok",
            "retcode": 0,
            "format": fmt,
            "filename": filename,
            "data": content,
        }

    # ------------------------------------------------------------------
    # Mutating endpoints --- all require an attested bot identity
    # ------------------------------------------------------------------
    async def _verify_application_bot(self, application_id: str, bot_self_id: str) -> str | None:
        """Verify the supplied bot id matches the application's attested identity."""
        row = await self.db.fetchone(
            "SELECT bot_self_id FROM applications WHERE id = ?", (application_id,)
        )
        if row is None:
            # Let the service layer produce its canonical "not found" message.
            return None
        stored = str(row["bot_self_id"] or "").strip()
        if not stored or stored.lower() == BOT_IDENTITY_PLACEHOLDER:
            return "申请记录缺少已验证的 Bot 身份，已拒绝操作"
        if stored != bot_self_id:
            return "bot_self_id 与申请记录的 Bot 身份不一致，已拒绝操作"
        return None

    async def approve_application(
        self,
        payload: dict[str, Any],
        actor_id: str = "dashboard_admin",
        actor_role: str = "DASHBOARD_ADMIN",
    ) -> dict[str, Any]:
        """API handler to approve an application (requires attested bot id)."""
        try:
            bot_self_id = _parse_bot_self_id(payload)
            app_id = _parse_application_id(payload.get("application_id"))
            note = _parse_note(payload.get("note"), "note", default="Dashboard管理员批准")
        except ParamError as exc:
            return self.err(str(exc))

        mismatch = await self._verify_application_bot(app_id, bot_self_id)
        if mismatch:
            return self.err(mismatch)

        act_id = actor_id if actor_id and actor_id != "WebUI" else "dashboard_admin"
        success, msg = await self.approval.approve_application(
            actor_id=act_id,
            actor_role=actor_role,
            application_id=app_id,
            note=note,
        )
        if not success:
            return self.err(msg)
        return self.ok({"message": msg, "application_id": app_id})

    async def reject_application(
        self,
        payload: dict[str, Any],
        actor_id: str = "dashboard_admin",
        actor_role: str = "DASHBOARD_ADMIN",
    ) -> dict[str, Any]:
        """API handler to reject an application (requires attested bot id)."""
        try:
            bot_self_id = _parse_bot_self_id(payload)
            app_id = _parse_application_id(payload.get("application_id"))
            reason = _parse_note(payload.get("reason"), "reason", default="Dashboard管理员拒绝")
        except ParamError as exc:
            return self.err(str(exc))

        mismatch = await self._verify_application_bot(app_id, bot_self_id)
        if mismatch:
            return self.err(mismatch)

        act_id = actor_id if actor_id and actor_id != "WebUI" else "dashboard_admin"
        success, msg = await self.approval.reject_application(
            actor_id=act_id,
            actor_role=actor_role,
            application_id=app_id,
            reason=reason,
        )
        if not success:
            return self.err(msg)
        return self.ok({"message": msg, "application_id": app_id})

    async def manual_grant(
        self,
        payload: dict[str, Any],
        actor_id: str = "dashboard_admin",
        actor_role: str = "DASHBOARD_ADMIN",
    ) -> dict[str, Any]:
        """API handler for manual grant (requires attested bot id)."""
        try:
            bot_self_id = _parse_bot_self_id(payload)
            group_id = _parse_group_id(payload.get("group_id"))
            user_id = _parse_digits(payload.get("user_id"), "user_id")
            duration_days = (
                _parse_present_int(payload, "duration_days", "duration_days", 0, MAX_DURATION_DAYS)
                if "duration_days" in payload
                else 0
            )
            note = _parse_note(payload.get("note"), "note", default="Dashboard手动授权")
            is_level_exempt = (
                _parse_strict_bool(payload["is_level_exempt"], "is_level_exempt")
                if "is_level_exempt" in payload
                else False
            )
        except ParamError as exc:
            return self.err(str(exc))

        act_id = actor_id if actor_id and actor_id != "WebUI" else "dashboard_admin"
        success, msg = await self.approval.grant_manually(
            actor_id=act_id,
            actor_role=actor_role,
            bot_self_id=bot_self_id,
            group_id=group_id,
            user_id=user_id,
            duration_days=duration_days,
            note=note,
            is_level_exempt=is_level_exempt,
        )
        if not success:
            return self.err(msg)
        return self.ok({"message": msg})

    async def revoke_grant(
        self,
        payload: dict[str, Any],
        actor_id: str = "dashboard_admin",
        actor_role: str = "DASHBOARD_ADMIN",
    ) -> dict[str, Any]:
        """API handler to revoke a grant (requires attested bot id)."""
        try:
            bot_self_id = _parse_bot_self_id(payload)
            group_id = _parse_group_id(payload.get("group_id"))
            user_id = _parse_digits(payload.get("user_id"), "user_id")
            reason = _parse_note(payload.get("reason"), "reason", default="Dashboard撤销授权")
        except ParamError as exc:
            return self.err(str(exc))

        act_id = actor_id if actor_id and actor_id != "WebUI" else "dashboard_admin"
        success, msg = await self.approval.revoke_grant(
            actor_id=act_id,
            actor_role=actor_role,
            bot_self_id=bot_self_id,
            group_id=group_id,
            user_id=user_id,
            reason=reason,
        )
        if not success:
            return self.err(msg)
        return self.ok({"message": msg})

    async def ban_user(
        self,
        payload: dict[str, Any],
        actor_id: str = "dashboard_admin",
        actor_role: str = "DASHBOARD_ADMIN",
    ) -> dict[str, Any]:
        """API handler to blacklist a user (requires attested bot id).

        ``group_id="ALL"`` is explicitly supported here for a global (per-bot)
        blacklist entry; every other endpoint rejects it.
        """
        try:
            bot_self_id = _parse_bot_self_id(payload)
            group_id = _parse_group_id(payload.get("group_id", "ALL"), allow_all=True)
            user_id = _parse_digits(payload.get("user_id"), "user_id")
            reason = _parse_note(payload.get("reason"), "reason", default="Dashboard拉黑")
            duration_days = (
                _parse_present_int(payload, "duration_days", "duration_days", 0, MAX_DURATION_DAYS)
                if "duration_days" in payload
                else 0
            )
        except ParamError as exc:
            return self.err(str(exc))

        act_id = actor_id if actor_id and actor_id != "WebUI" else "dashboard_admin"
        success, msg = await self.approval.ban_user(
            actor_id=act_id,
            actor_role=actor_role,
            bot_self_id=bot_self_id,
            group_id=group_id,
            user_id=user_id,
            reason=reason,
            duration_days=duration_days,
        )
        if not success:
            return self.err(msg)
        return self.ok({"message": msg})

    async def unban_user(
        self,
        payload: dict[str, Any],
        actor_id: str = "dashboard_admin",
        actor_role: str = "DASHBOARD_ADMIN",
    ) -> dict[str, Any]:
        """API handler to unban a user (requires attested bot id)."""
        try:
            bot_self_id = _parse_bot_self_id(payload)
            group_id = _parse_group_id(payload.get("group_id", "ALL"), allow_all=True)
            user_id = _parse_digits(payload.get("user_id"), "user_id")
        except ParamError as exc:
            return self.err(str(exc))

        act_id = actor_id if actor_id and actor_id != "WebUI" else "dashboard_admin"
        success, msg = await self.approval.unban_user(
            actor_id=act_id,
            actor_role=actor_role,
            bot_self_id=bot_self_id,
            group_id=group_id,
            user_id=user_id,
        )
        if not success:
            return self.err(msg)
        return self.ok({"message": msg})

    async def update_group_policy(
        self,
        payload: dict[str, Any],
        actor_id: str = "dashboard_admin",
        actor_role: str = "DASHBOARD_ADMIN",
    ) -> dict[str, Any]:
        """Update or insert a group policy with strict validation/attested bot id.

        ``actor_id`` / ``actor_role`` are accepted (and required by the route
        layer's generic authorized-mutation helper) but the policy write itself
        is authorized upstream; this method never derives or widens a role.
        """
        try:
            bot_self_id = _parse_bot_self_id(payload)
            group_id = _parse_group_id(payload.get("group_id"))
            enabled = _parse_strict_bool(payload["enabled"], "enabled") if "enabled" in payload else None
            min_level = _parse_present_int(payload, "min_level", "min_level", 0, MAX_MIN_LEVEL)
            approval_required = (
                _parse_strict_bool(payload["approval_required"], "approval_required")
                if "approval_required" in payload
                else None
            )
            grant_duration_days = _parse_present_int(
                payload, "grant_duration_days", "grant_duration_days", 0, MAX_DURATION_DAYS
            )
            recheck_interval_seconds = _parse_present_int(
                payload,
                "recheck_interval_seconds",
                "recheck_interval_seconds",
                MIN_RECHECK_INTERVAL_SECONDS,
                MAX_RECHECK_INTERVAL_SECONDS,
            )
            restore_policy = (
                _parse_restore_policy(payload["restore_policy"]) if "restore_policy" in payload else None
            )
            owner_bypass = (
                _parse_strict_bool(payload["owner_bypass"], "owner_bypass") if "owner_bypass" in payload else None
            )
            group_admin_bypass = (
                _parse_strict_bool(payload["group_admin_bypass"], "group_admin_bypass")
                if "group_admin_bypass" in payload
                else None
            )
        except ParamError as exc:
            return self.err(str(exc))

        pol = await self.approval.get_group_policy(bot_self_id, group_id)
        if enabled is not None:
            pol.enabled = enabled
        if min_level is not None:
            pol.min_level = min_level
        if approval_required is not None:
            pol.approval_required = approval_required
        if grant_duration_days is not None:
            pol.grant_duration_seconds = grant_duration_days * 86400
        if recheck_interval_seconds is not None:
            pol.recheck_interval_seconds = recheck_interval_seconds
        if restore_policy is not None:
            pol.restore_policy = restore_policy
        if owner_bypass is not None:
            pol.owner_bypass = owner_bypass
        if group_admin_bypass is not None:
            pol.group_admin_bypass = group_admin_bypass

        await self.approval.save_group_policy(pol)
        return self.ok({"message": f"群 {group_id} 策略已更新", "policy": pol.to_dict()})
