"""REST API handlers for AstrBot WebUI management views."""

from __future__ import annotations

import logging
from typing import Any

from .approval_service import ApprovalService
from .audit_service import AuditService
from .config import PluginConfig
from .database import Database

logger = logging.getLogger("astrbot_plugin_group_approval.web_api")


def parse_bool(val: Any, default: bool = False) -> bool:
    """Strict boolean coercion avoiding 'false' == True pitfalls."""
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


class WebApiHandler:
    """Provides REST endpoints consumed by the Plugin Pages dashboard."""

    def __init__(
        self,
        db: Database,
        approval_service: ApprovalService,
        audit_service: AuditService,
        config: PluginConfig,
    ):
        self.db = db
        self.approval = approval_service
        self.audit = audit_service
        self.config = config

    # Helper response constructors
    def ok(self, data: Any = None) -> dict[str, Any]:
        return {"status": "ok", "retcode": 0, "data": data if data is not None else {}}

    def err(self, message: str, retcode: int = 400) -> dict[str, Any]:
        return {"status": "error", "retcode": retcode, "message": message}

    async def list_bots(self, context: Any = None) -> dict[str, Any]:
        """Return connected and known bot IDs."""
        bots: set[str] = set()
        if context is not None:
            pm = getattr(context, "platform_manager", None)
            if pm is not None:
                get_insts = getattr(pm, "get_insts", None)
                if callable(get_insts):
                    try:
                        for inst in get_insts():
                            bid = str(getattr(inst, "self_id", None) or "").strip()
                            if bid:
                                bots.add(bid)
                    except Exception:
                        pass
        # Also query known bot IDs from group_policies and grants
        p_rows = await self.db.fetchall("SELECT DISTINCT bot_self_id FROM group_policies")
        for r in p_rows:
            bid = str(r["bot_self_id"] or "").strip()
            if bid and bid != "default":
                bots.add(bid)
        g_rows = await self.db.fetchall("SELECT DISTINCT bot_self_id FROM grants")
        for r in g_rows:
            bid = str(r["bot_self_id"] or "").strip()
            if bid and bid != "default":
                bots.add(bid)

        return self.ok({"bots": sorted(list(bots))})

    async def get_dashboard_stats(self) -> dict[str, Any]:
        """Summary metrics for the dashboard view."""
        g_row = await self.db.fetchone("SELECT COUNT(*) FROM group_policies WHERE enabled = 1")
        p_row = await self.db.fetchone("SELECT COUNT(*) FROM applications WHERE status = 'PENDING'")
        a_row = await self.db.fetchone("SELECT COUNT(*) FROM grants WHERE status = 'ACTIVE'")
        s_row = await self.db.fetchone("SELECT COUNT(*) FROM grants WHERE status = 'SUSPENDED'")
        b_row = await self.db.fetchone("SELECT COUNT(*) FROM blacklist")

        # Recent approvals
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
        """Fetch applications with filtering."""
        clauses = []
        params = []
        if bot_self_id:
            clauses.append("bot_self_id = ?")
            params.append(bot_self_id.strip())
        if group_id:
            clauses.append("group_id = ?")
            params.append(group_id.strip())
        if status:
            clauses.append("status = ?")
            params.append(status.strip().upper())
        if user_id:
            clauses.append("user_id = ?")
            params.append(user_id.strip())

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

    async def approve_application(
        self,
        payload: dict[str, Any],
        actor_id: str = "dashboard_admin",
        actor_role: str = "DASHBOARD_ADMIN",
    ) -> dict[str, Any]:
        """API handler to approve an application."""
        app_id = str(payload.get("application_id", "")).strip()
        note = str(payload.get("note", "Dashboard管理员批准")).strip()

        if not app_id:
            return self.err("缺少 application_id 参数")

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
        """API handler to reject an application."""
        app_id = str(payload.get("application_id", "")).strip()
        reason = str(payload.get("reason", "Dashboard管理员拒绝")).strip()

        if not app_id:
            return self.err("缺少 application_id 参数")

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

    async def list_users(
        self,
        group_id: str | None = None,
        user_id: str | None = None,
        status: str | None = None,
        bot_self_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List user grants."""
        clauses = []
        params = []
        if bot_self_id:
            clauses.append("bot_self_id = ?")
            params.append(bot_self_id.strip())
        if group_id:
            clauses.append("group_id = ?")
            params.append(group_id.strip())
        if user_id:
            clauses.append("user_id = ?")
            params.append(user_id.strip())
        if status:
            clauses.append("status = ?")
            params.append(status.strip().upper())

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

    async def manual_grant(
        self,
        payload: dict[str, Any],
        actor_id: str = "dashboard_admin",
        actor_role: str = "DASHBOARD_ADMIN",
    ) -> dict[str, Any]:
        """API handler for manual grant."""
        group_id = str(payload.get("group_id", "")).strip()
        user_id = str(payload.get("user_id", "")).strip()
        bot_self_id = str(payload.get("bot_self_id", "")).strip() or "default"
        duration_days = int(payload.get("duration_days", 0))
        note = str(payload.get("note", "Dashboard手动授权")).strip()
        is_level_exempt = parse_bool(payload.get("is_level_exempt", False))

        if not group_id or not user_id:
            return self.err("缺少 group_id 或 user_id 参数")

        act_id = actor_id if actor_id and actor_id != "WebUI" else "dashboard_admin"
        success, msg = await self.approval.grant_manually(
            actor_id=act_id,
            actor_role="OWNER" if actor_role == "DASHBOARD_ADMIN" else actor_role,
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
        """API handler to revoke a grant."""
        group_id = str(payload.get("group_id", "")).strip()
        user_id = str(payload.get("user_id", "")).strip()
        bot_self_id = str(payload.get("bot_self_id", "default")).strip()
        reason = str(payload.get("reason", "Dashboard撤销授权")).strip()

        if not group_id or not user_id:
            return self.err("缺少 group_id 或 user_id 参数")

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
        """API handler to blacklist a user."""
        group_id = str(payload.get("group_id", "ALL")).strip()
        user_id = str(payload.get("user_id", "")).strip()
        bot_self_id = str(payload.get("bot_self_id", "default")).strip()
        reason = str(payload.get("reason", "Dashboard拉黑")).strip()
        days = int(payload.get("duration_days", 0))

        if not user_id:
            return self.err("缺少 user_id 参数")

        act_id = actor_id if actor_id and actor_id != "WebUI" else "dashboard_admin"
        success, msg = await self.approval.ban_user(
            actor_id=act_id,
            actor_role="OWNER" if actor_role == "DASHBOARD_ADMIN" else actor_role,
            bot_self_id=bot_self_id,
            group_id=group_id,
            user_id=user_id,
            reason=reason,
            duration_days=days,
        )
        return self.ok({"message": msg})

    async def unban_user(
        self,
        payload: dict[str, Any],
        actor_id: str = "dashboard_admin",
        actor_role: str = "DASHBOARD_ADMIN",
    ) -> dict[str, Any]:
        """API handler to unban user."""
        group_id = str(payload.get("group_id", "ALL")).strip()
        user_id = str(payload.get("user_id", "")).strip()
        bot_self_id = str(payload.get("bot_self_id", "default")).strip()

        if not user_id:
            return self.err("缺少 user_id 参数")

        act_id = actor_id if actor_id and actor_id != "WebUI" else "dashboard_admin"
        success, msg = await self.approval.unban_user(
            actor_id=act_id,
            actor_role="OWNER" if actor_role == "DASHBOARD_ADMIN" else actor_role,
            bot_self_id=bot_self_id,
            group_id=group_id,
            user_id=user_id,
        )
        if not success:
            return self.err(msg)
        return self.ok({"message": msg})

    async def list_groups(self, bot_self_id: str | None = None) -> dict[str, Any]:
        """List configured group policies with optional bot_self_id filter."""
        if bot_self_id:
            rows = await self.db.fetchall(
                "SELECT * FROM group_policies WHERE bot_self_id = ? ORDER BY created_at DESC",
                (bot_self_id.strip(),),
            )
        else:
            rows = await self.db.fetchall("SELECT * FROM group_policies ORDER BY created_at DESC")
        return self.ok({"groups": [dict(r) for r in rows]})

    async def update_group_policy(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Update or insert group policy with strict parameter validation."""
        group_id = str(payload.get("group_id", "")).strip()
        bot_self_id = str(payload.get("bot_self_id", "")).strip() or "default"

        if not group_id:
            return self.err("缺少 group_id 参数")

        pol = await self.approval.get_group_policy(bot_self_id, group_id)
        if "enabled" in payload:
            pol.enabled = parse_bool(payload["enabled"])
        if "min_level" in payload:
            try:
                lvl = int(payload["min_level"])
                if lvl < 0:
                    return self.err("min_level 不能为负数")
                pol.min_level = lvl
            except (ValueError, TypeError):
                return self.err("min_level 必须为有效整数")
        if "approval_required" in payload:
            pol.approval_required = parse_bool(payload["approval_required"])
        if "grant_duration_days" in payload:
            try:
                days = int(payload["grant_duration_days"])
                if days < 0:
                    return self.err("grant_duration_days 不能为负数")
                pol.grant_duration_seconds = days * 86400
            except (ValueError, TypeError):
                return self.err("grant_duration_days 必须为有效整数")
        if "recheck_interval_seconds" in payload:
            try:
                interval = int(payload["recheck_interval_seconds"])
                if interval < 10:
                    return self.err("recheck_interval_seconds 不能小于 10 秒")
                pol.recheck_interval_seconds = interval
            except (ValueError, TypeError):
                return self.err("recheck_interval_seconds 必须为有效整数")
        if "restore_policy" in payload:
            pol.restore_policy = str(payload["restore_policy"])
        if "owner_bypass" in payload:
            pol.owner_bypass = parse_bool(payload["owner_bypass"])
        if "group_admin_bypass" in payload:
            pol.group_admin_bypass = parse_bool(payload["group_admin_bypass"])

        await self.approval.save_group_policy(pol)
        return self.ok({"message": f"群 {group_id} 策略已更新", "policy": pol.to_dict()})

    async def list_audit_logs(
        self,
        group_id: str | None = None,
        user_id: str | None = None,
        action: str | None = None,
        bot_self_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Fetch audit log entries."""
        logs = await self.audit.query(
            bot_self_id=bot_self_id,
            group_id=group_id,
            target_user_id=user_id,
            action=action,
            limit=limit,
            offset=offset,
        )
        return self.ok({"audit_logs": [e.to_dict() for e in logs]})

    async def export_audit_logs(
        self,
        format_type: str = "json",
        bot_self_id: str | None = None,
        group_id: str | None = None,
        action: str | None = None,
        start_time: int | None = None,
        end_time: int | None = None,
        limit: int = 5000,
    ) -> str:
        """Export audit logs as JSON or CSV with multi-criteria filtering."""
        kwargs: dict[str, Any] = {
            "bot_self_id": bot_self_id,
            "group_id": group_id,
            "action": action,
            "start_time": start_time,
            "end_time": end_time,
            "limit": limit,
        }
        if format_type.lower() == "csv":
            return await self.audit.export_csv(**kwargs)
        return await self.audit.export_json(**kwargs)
