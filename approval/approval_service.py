"""Core business logic for applications, approval state machine, and grants."""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid

from .audit_service import AuditService
from .config import PluginConfig
from .database import Database
from .level_service import LevelService
from .models import (
    Application,
    ApplicationStatus,
    Grant,
    GrantStatus,
    GroupPolicy,
)
from .notification_service import NotificationService

logger = logging.getLogger("astrbot_plugin_group_approval.approval")


class ApprovalService:
    """Manages the lifecycle of access applications and grants."""

    def __init__(
        self,
        db: Database,
        level_service: LevelService,
        audit_service: AuditService,
        notification_service: NotificationService,
        config: PluginConfig,
    ):
        self.db = db
        self.level_service = level_service
        self.audit = audit_service
        self.notifications = notification_service
        self.config = config
        self._action_lock = asyncio.Lock()

    async def get_group_policy(self, bot_self_id: str, group_id: str) -> GroupPolicy:
        """Fetch group policy from database or fall back to configuration/default."""
        b_id = str(bot_self_id).strip()
        g_id = str(group_id).strip()

        row = await self.db.fetchone(
            "SELECT * FROM group_policies WHERE bot_self_id = ? AND group_id = ?",
            (b_id, g_id),
        )

        if row is not None:
            return GroupPolicy(
                bot_self_id=row["bot_self_id"],
                group_id=row["group_id"],
                enabled=bool(row["enabled"]),
                min_level=int(row["min_level"]),
                approval_required=bool(row["approval_required"]),
                grant_duration_seconds=int(row["grant_duration_seconds"]),
                recheck_interval_seconds=int(row["recheck_interval_seconds"]),
                restore_policy=row["restore_policy"],
                owner_bypass=bool(row["owner_bypass"]),
                group_admin_bypass=bool(row["group_admin_bypass"]),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )

        # Check in-memory config for preconfigured group
        cfg_group = self.config.groups.get(g_id)
        if cfg_group:
            now = int(time.time())
            pol = GroupPolicy(
                bot_self_id=b_id,
                group_id=g_id,
                enabled=bool(cfg_group.get("enabled", True)),
                min_level=int(cfg_group.get("min_level", 10)),
                approval_required=bool(cfg_group.get("approval_required", True)),
                grant_duration_seconds=int(cfg_group.get("grant_duration_days", 0)) * 86400,
                recheck_interval_seconds=int(cfg_group.get("recheck_interval_seconds", 300)),
                restore_policy=str(cfg_group.get("restore_policy", "automatic")),
                owner_bypass=bool(cfg_group.get("owner_bypass", True)),
                group_admin_bypass=bool(cfg_group.get("group_admin_bypass", False)),
                created_at=now,
                updated_at=now,
            )
            # Persist to database so subsequent queries find it
            await self.save_group_policy(pol)
            return pol

        # Default policy
        now = int(time.time())
        default_enabled = (self.config.default_policy == "allow")
        pol = GroupPolicy(
            bot_self_id=b_id,
            group_id=g_id,
            enabled=default_enabled,
            min_level=10,
            approval_required=True,
            grant_duration_seconds=0,
            recheck_interval_seconds=300,
            restore_policy="automatic",
            owner_bypass=True,
            group_admin_bypass=False,
            created_at=now,
            updated_at=now,
        )
        return pol

    async def save_group_policy(self, policy: GroupPolicy) -> None:
        """Upsert a group policy."""
        now = int(time.time())
        sql = """
        INSERT INTO group_policies (
            bot_self_id, group_id, enabled, min_level, approval_required,
            grant_duration_seconds, recheck_interval_seconds, restore_policy,
            owner_bypass, group_admin_bypass, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(bot_self_id, group_id) DO UPDATE SET
            enabled = excluded.enabled,
            min_level = excluded.min_level,
            approval_required = excluded.approval_required,
            grant_duration_seconds = excluded.grant_duration_seconds,
            recheck_interval_seconds = excluded.recheck_interval_seconds,
            restore_policy = excluded.restore_policy,
            owner_bypass = excluded.owner_bypass,
            group_admin_bypass = excluded.group_admin_bypass,
            updated_at = excluded.updated_at
        """
        params = (
            policy.bot_self_id,
            policy.group_id,
            1 if policy.enabled else 0,
            policy.min_level,
            1 if policy.approval_required else 0,
            policy.grant_duration_seconds,
            policy.recheck_interval_seconds,
            policy.restore_policy,
            1 if policy.owner_bypass else 0,
            1 if policy.group_admin_bypass else 0,
            policy.created_at or now,
            now,
        )
        await self.db.execute(sql, params)
        await self.db.commit()

    async def is_blacklisted(self, bot_self_id: str, group_id: str, user_id: str) -> tuple[bool, str]:
        """Check if user is blacklisted either specifically in group or globally."""
        now = int(time.time())
        sql = """
        SELECT reason, expires_at FROM blacklist
        WHERE bot_self_id = ? AND user_id = ? AND (group_id = ? OR group_id = 'ALL')
        """
        rows = await self.db.fetchall(sql, (bot_self_id, user_id, group_id))
        for r in rows:
            exp = r["expires_at"]
            if exp == 0 or exp > now:
                return True, r["reason"] or "您已被列入黑名单"
        return False, ""

    async def get_active_grant(self, bot_self_id: str, group_id: str, user_id: str) -> Grant | None:
        """Retrieve active grant if one exists."""
        row = await self.db.fetchone(
            "SELECT * FROM grants WHERE bot_self_id = ? AND group_id = ? AND user_id = ?",
            (bot_self_id, group_id, user_id),
        )
        if row is None:
            return None

        keys = row.keys() if hasattr(row, "keys") else ()
        grant = Grant(
            id=row["id"],
            bot_self_id=row["bot_self_id"],
            group_id=row["group_id"],
            user_id=row["user_id"],
            status=GrantStatus(row["status"]),
            source_application_id=row["source_application_id"],
            approved_by=row["approved_by"],
            approved_at=row["approved_at"],
            expires_at=row["expires_at"],
            last_verified_level=row["last_verified_level"],
            last_verified_at=row["last_verified_at"],
            suspend_reason=row["suspend_reason"],
            revoked_by=row["revoked_by"],
            revoked_at=row["revoked_at"],
            grant_type=row["grant_type"] if "grant_type" in keys else "LEVEL_APPROVAL",
            is_level_exempt=bool(row["is_level_exempt"]) if "is_level_exempt" in keys else False,
        )
        return grant

    async def get_pending_application(self, bot_self_id: str, group_id: str, user_id: str) -> Application | None:
        """Retrieve existing pending application."""
        row = await self.db.fetchone(
            "SELECT * FROM applications WHERE bot_self_id = ? AND group_id = ? AND user_id = ? AND status = 'PENDING'",
            (bot_self_id, group_id, user_id),
        )
        if row is None:
            return None

        return Application(
            id=row["id"],
            bot_self_id=row["bot_self_id"],
            group_id=row["group_id"],
            user_id=row["user_id"],
            level_at_application=row["level_at_application"],
            status=ApplicationStatus(row["status"]),
            reason=row["reason"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            reviewed_at=row["reviewed_at"],
            reviewer_id=row["reviewer_id"],
            review_note=row["review_note"],
        )

    async def _generate_app_id(self) -> str:
        """Generate unique readable application ID, e.g. A1024."""
        for _ in range(10):
            num = random.randint(1000, 9999)
            code = f"A{num}"
            row = await self.db.fetchone("SELECT id FROM applications WHERE id = ?", (code,))
            if row is None:
                return code
        return f"A{int(time.time()) % 100000}"

    async def apply_for_access(
        self,
        bot_self_id: str,
        group_id: str,
        user_id: str,
        reason: str = "",
    ) -> tuple[bool, str, Application | None]:
        """Submit a new access application."""
        b_id = str(bot_self_id).strip()
        g_id = str(group_id).strip()
        u_id = str(user_id).strip()

        # 1. Check if group is managed and enabled
        policy = await self.get_group_policy(b_id, g_id)
        if not policy.enabled:
            return False, "当前群尚未开启 Bot 使用准入机制", None

        # 2. Check blacklist
        is_banned, ban_reason = await self.is_blacklisted(b_id, g_id, u_id)
        if is_banned:
            return False, f"申请被拒绝：{ban_reason}", None

        # 3. Check existing active grant
        grant = await self.get_active_grant(b_id, g_id, u_id)
        if grant is not None and grant.status == GrantStatus.ACTIVE:
            now = int(time.time())
            if grant.expires_at == 0 or grant.expires_at > now:
                return False, "您已拥有当前群的 Bot 使用权限，无需重复申请", None

        # 4. Check existing pending application
        pending = await self.get_pending_application(b_id, g_id, u_id)
        if pending is not None:
            return False, f"您已有待审核的申请（编号：{pending.id}），请等待管理员审批", pending

        # 5. Check reject cooldown
        now = int(time.time())

        # Check rate limit
        rate_key = f"rate:user:{b_id}:{g_id}:{u_id}"
        rate_limit = getattr(self.config.application, "rate_limit_per_hour", 5)
        if rate_limit > 0:
            allowed = await self.check_and_increment_rate_limit(rate_key, rate_limit, window_seconds=3600)
            if not allowed:
                return False, f"申请过于频繁：每小时最多提交 {rate_limit} 次申请，请稍后再试", None

        cooldown_sec = self.config.application.reject_cooldown_hours * 3600
        recent_reject = await self.db.fetchone(
            """
            SELECT reviewed_at, review_note FROM applications
            WHERE bot_self_id = ? AND group_id = ? AND user_id = ? AND status = 'REJECTED'
            ORDER BY reviewed_at DESC LIMIT 1
            """,
            (b_id, g_id, u_id),
        )
        if recent_reject and recent_reject["reviewed_at"]:
            passed = now - recent_reject["reviewed_at"]
            if passed < cooldown_sec:
                hours_left = max(1, int((cooldown_sec - passed) / 3600))
                return (
                    False,
                    f"申请冷却中：您最近的申请曾被拒绝（原因：{recent_reject['review_note'] or '未说明'}），请在 {hours_left} 小时后再试",
                    None,
                )

        # 6. Query real member level from NapCat with force_refresh=True
        lvl_res = await self.level_service.get_group_member_level(
            bot_id=b_id,
            group_id=g_id,
            user_id=u_id,
            force_refresh=True,
        )

        if not lvl_res.success or not lvl_res.is_member:
            return False, f"获取群成员信息失败（错误代码：{lvl_res.error_code or 'UNKNOWN'}），请稍后重试", None

        # 7. Compare member level with group min_level
        if lvl_res.level < policy.min_level:
            return (
                False,
                f"当前群申请门槛为 LV{policy.min_level}，您当前群等级为 LV{lvl_res.level}，未达到申请要求",
                None,
            )

        # 8. Create application record
        async with self._action_lock:
            # Re-check under lock
            pending = await self.get_pending_application(b_id, g_id, u_id)
            if pending is not None:
                return False, f"您已有待审核的申请（编号：{pending.id}）", pending

            app_id = await self._generate_app_id()
            exp_sec = self.config.application.expire_hours * 3600
            expires_at = now + exp_sec

            async with self.db.transaction():
                sql = """
                INSERT INTO applications (
                    id, bot_self_id, group_id, user_id, level_at_application,
                    status, reason, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?, ?)
                """
                await self.db.execute(
                    sql,
                    (app_id, b_id, g_id, u_id, lvl_res.level, reason, now, expires_at),
                )

        app = Application(
            id=app_id,
            bot_self_id=b_id,
            group_id=g_id,
            user_id=u_id,
            level_at_application=lvl_res.level,
            status=ApplicationStatus.PENDING,
            reason=reason,
            created_at=now,
            expires_at=expires_at,
        )

        # 9. Audit log
        await self.audit.log(
            actor_id=u_id,
            actor_type="USER",
            bot_self_id=b_id,
            group_id=g_id,
            target_user_id=u_id,
            action="APPLICATION_SUBMITTED",
            old_state=None,
            new_state="PENDING",
            request_id=app_id,
            details={"level": lvl_res.level, "min_level": policy.min_level, "reason": reason},
        )

        # 10. Notify admins
        admin_recipients = list(set(self.config.owner_qq_ids + self.config.reviewer_qq_ids))
        await self.notifications.notify_admins_of_application(
            app_id=app_id,
            user_id=u_id,
            group_id=g_id,
            level=lvl_res.level,
            min_level=policy.min_level,
            recipients=admin_recipients,
            bot_self_id=b_id,
            fallback_group=self.config.notifications.fallback_group_id,
        )

        return (
            True,
            f"申请已提交（编号：{app_id}）！\n当前群等级：LV{lvl_res.level}（门槛：LV{policy.min_level}）。请等待管理员审批。",
            app,
        )

    async def approve_application(
        self,
        actor_id: str,
        actor_role: str,
        application_id: str,
        note: str = "",
    ) -> tuple[bool, str]:
        """Approve an application with atomic SQLite transaction and concurrency protection."""
        app_id = str(application_id).strip()
        now = int(time.time())

        async with self._action_lock:
            # 1. Fetch application
            row = await self.db.fetchone("SELECT * FROM applications WHERE id = ?", (app_id,))
            if row is None:
                return False, f"申请编号 {app_id} 不存在"

            current_status = row["status"]
            if current_status != "PENDING":
                return False, f"该申请已被处理（当前状态：{current_status}）"

            # Real-time expiration check (expires_at <= now)
            exp_at = row["expires_at"]
            if exp_at > 0 and exp_at <= now:
                await self.db.execute(
                    "UPDATE applications SET status = 'EXPIRED' WHERE id = ? AND status = 'PENDING'",
                    (app_id,),
                )
                await self.db.commit()
                return False, f"该申请已于 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(exp_at))} 过期失效，无法批准"

            bot_self_id = row["bot_self_id"]
            group_id = row["group_id"]
            user_id = row["user_id"]

            policy = await self.get_group_policy(bot_self_id, group_id)

            # 2. Force re-check current member level and group membership before granting
            lvl_res = await self.level_service.get_group_member_level(
                bot_id=bot_self_id,
                group_id=group_id,
                user_id=user_id,
                force_refresh=True,
            )

            if not lvl_res.success or not lvl_res.is_member:
                return (
                    False,
                    f"批准失败：无法获取用户当前群成员信息（可能已退群，错误码：{lvl_res.error_code}）",
                )

            if lvl_res.level < policy.min_level:
                return (
                    False,
                    f"批准失败：用户当前群等级 (LV{lvl_res.level}) 低于本群门槛 (LV{policy.min_level})",
                )

            # 3. Calculate grant expiration
            grant_exp = 0
            if policy.grant_duration_seconds > 0:
                grant_exp = now + policy.grant_duration_seconds

            grant_id = f"G{app_id.removeprefix('A')}"

            # 4. Atomic transaction update (Application + Grant + Audit + Outbox Notification)
            try:
                async with self.db.transaction():
                    # Update application
                    await self.db.execute(
                        """
                        UPDATE applications
                        SET status = 'APPROVED', reviewed_at = ?, reviewer_id = ?, review_note = ?
                        WHERE id = ? AND status = 'PENDING'
                        """,
                        (now, str(actor_id).strip(), note, app_id),
                    )

                    # Upsert grant
                    sql_grant = """
                    INSERT INTO grants (
                        id, bot_self_id, group_id, user_id, status, source_application_id,
                        approved_by, approved_at, expires_at, last_verified_level, last_verified_at,
                        suspend_reason, revoked_by, revoked_at, grant_type, is_level_exempt
                    ) VALUES (?, ?, ?, ?, 'ACTIVE', ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, 'LEVEL_APPROVAL', 0)
                    ON CONFLICT(bot_self_id, group_id, user_id) DO UPDATE SET
                        id = excluded.id,
                        status = 'ACTIVE',
                        source_application_id = excluded.source_application_id,
                        approved_by = excluded.approved_by,
                        approved_at = excluded.approved_at,
                        expires_at = excluded.expires_at,
                        last_verified_level = excluded.last_verified_level,
                        last_verified_at = excluded.last_verified_at,
                        suspend_reason = NULL,
                        revoked_by = NULL,
                        revoked_at = NULL,
                        grant_type = 'LEVEL_APPROVAL',
                        is_level_exempt = 0
                    """
                    await self.db.execute(
                        sql_grant,
                        (
                            grant_id,
                            bot_self_id,
                            group_id,
                            user_id,
                            app_id,
                            str(actor_id).strip(),
                            now,
                            grant_exp,
                            lvl_res.level,
                            now,
                        ),
                    )

                    # Audit log inside same transaction
                    await self.audit.log(
                        actor_id=str(actor_id),
                        actor_type=actor_role,
                        bot_self_id=bot_self_id,
                        group_id=group_id,
                        target_user_id=user_id,
                        action="APPLICATION_APPROVED",
                        old_state="PENDING",
                        new_state="APPROVED",
                        request_id=app_id,
                        details={"grant_id": grant_id, "note": note, "verified_level": lvl_res.level},
                    )

                    # Enqueue user notification outbox inside same transaction
                    await self.notifications.notify_user_result(
                        user_id=user_id,
                        group_id=group_id,
                        approved=True,
                        note=note,
                        bot_self_id=bot_self_id,
                    )
            except Exception as e:
                logger.error(f"Transaction failed during approve_application {app_id}: {e}")
                return False, f"数据库事务异常：{e}"

        return True, f"已成功批准申请 {app_id}，用户 {user_id} 已获得本群 Bot 使用权限！"

    async def reject_application(
        self,
        actor_id: str,
        actor_role: str,
        application_id: str,
        reason: str = "",
    ) -> tuple[bool, str]:
        """Reject an application."""
        app_id = str(application_id).strip()
        now = int(time.time())

        async with self._action_lock:
            row = await self.db.fetchone("SELECT * FROM applications WHERE id = ?", (app_id,))
            if row is None:
                return False, f"申请编号 {app_id} 不存在"

            current_status = row["status"]
            if current_status != "PENDING":
                return False, f"该申请已被处理（当前状态：{current_status}）"

            # Real-time expiration check (expires_at <= now)
            exp_at = row["expires_at"]
            if exp_at > 0 and exp_at <= now:
                await self.db.execute(
                    "UPDATE applications SET status = 'EXPIRED' WHERE id = ? AND status = 'PENDING'",
                    (app_id,),
                )
                await self.db.commit()
                return False, "该申请已过期失效，无需拒绝"

            bot_self_id = row["bot_self_id"]
            group_id = row["group_id"]
            user_id = row["user_id"]

            async with self.db.transaction():
                await self.db.execute(
                    """
                    UPDATE applications
                    SET status = 'REJECTED', reviewed_at = ?, reviewer_id = ?, review_note = ?
                    WHERE id = ? AND status = 'PENDING'
                    """,
                    (now, str(actor_id).strip(), reason, app_id),
                )
                # Audit log
                await self.audit.log(
                    actor_id=str(actor_id),
                    actor_type=actor_role,
                    bot_self_id=bot_self_id,
                    group_id=group_id,
                    target_user_id=user_id,
                    action="APPLICATION_REJECTED",
                    old_state="PENDING",
                    new_state="REJECTED",
                    request_id=app_id,
                    details={"reason": reason},
                )
                # Notify user
                await self.notifications.notify_user_result(
                    user_id=user_id,
                    group_id=group_id,
                    approved=False,
                    note=reason,
                    bot_self_id=bot_self_id,
                )

        return True, f"已拒绝申请 {app_id}（原因：{reason or '未说明'}）"

    async def cancel_application(self, user_id: str, bot_self_id: str, group_id: str) -> tuple[bool, str]:
        """Allow user to cancel their own pending application."""
        u_id = str(user_id).strip()
        b_id = str(bot_self_id).strip()
        g_id = str(group_id).strip()

        async with self._action_lock:
            app = await self.get_pending_application(b_id, g_id, u_id)
            if app is None:
                return False, "您在当前群没有正在等待审核的申请"

            await self.db.execute(
                "UPDATE applications SET status = 'CANCELLED' WHERE id = ? AND status = 'PENDING'",
                (app.id,),
            )
            await self.db.commit()

        await self.audit.log(
            actor_id=u_id,
            actor_type="USER",
            bot_self_id=b_id,
            group_id=g_id,
            target_user_id=u_id,
            action="APPLICATION_CANCELLED",
            old_state="PENDING",
            new_state="CANCELLED",
            request_id=app.id,
        )
        return True, f"您的申请（编号：{app.id}）已成功取消"

    async def revoke_grant(
        self,
        actor_id: str,
        actor_role: str,
        bot_self_id: str,
        group_id: str,
        user_id: str,
        reason: str = "",
    ) -> tuple[bool, str]:
        """Revoke an active or suspended grant."""
        b_id = str(bot_self_id).strip()
        g_id = str(group_id).strip()
        u_id = str(user_id).strip()
        now = int(time.time())

        async with self._action_lock:
            grant = await self.get_active_grant(b_id, g_id, u_id)
            if grant is None or grant.status == GrantStatus.REVOKED:
                return False, f"用户 {u_id} 在群 {g_id} 并没有有效的授权记录"

            old_state = grant.status.value
            await self.db.execute(
                """
                UPDATE grants
                SET status = 'REVOKED', revoked_by = ?, revoked_at = ?, suspend_reason = ?
                WHERE bot_self_id = ? AND group_id = ? AND user_id = ?
                """,
                (str(actor_id).strip(), now, reason, b_id, g_id, u_id),
            )
            await self.db.commit()

        await self.audit.log(
            actor_id=str(actor_id),
            actor_type=actor_role,
            bot_self_id=b_id,
            group_id=g_id,
            target_user_id=u_id,
            action="GRANT_REVOKED",
            old_state=old_state,
            new_state="REVOKED",
            details={"reason": reason},
        )
        return True, f"已成功撤销用户 {u_id} 在群 {g_id} 的使用权限"

    async def grant_manually(
        self,
        actor_id: str,
        actor_role: str,
        bot_self_id: str,
        group_id: str,
        user_id: str,
        duration_days: int = 0,
        note: str = "",
        is_level_exempt: bool = False,
    ) -> tuple[bool, str]:
        """Manually grant access without an application."""
        b_id = str(bot_self_id).strip()
        g_id = str(group_id).strip()
        u_id = str(user_id).strip()
        now = int(time.time())

        # Check blacklist
        is_banned, ban_reason = await self.is_blacklisted(b_id, g_id, u_id)
        if is_banned:
            return False, f"手动授权失败：该用户处于黑名单中（{ban_reason}）"

        if is_level_exempt and actor_role != "OWNER":
            return False, "权限不足：仅机器人主人(OWNER)可以授予免等级检查权限"

        # Discontinue fake level 999: default to 0 (or previous verified level if re-granting)
        existing = await self.get_active_grant(b_id, g_id, u_id)
        actual_level = existing.last_verified_level if existing else 0

        exp_at = (now + duration_days * 86400) if duration_days > 0 else 0
        grant_id = f"G_MANUAL_{uuid.uuid4().hex[:12]}"
        grant_type = "LEVEL_EXEMPTION" if is_level_exempt else "OWNER_MANUAL"

        async with self._action_lock:
            async with self.db.transaction():
                sql = """
                INSERT INTO grants (
                    id, bot_self_id, group_id, user_id, status, source_application_id,
                    approved_by, approved_at, expires_at, last_verified_level, last_verified_at,
                    grant_type, is_level_exempt
                ) VALUES (?, ?, ?, ?, 'ACTIVE', 'MANUAL', ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bot_self_id, group_id, user_id) DO UPDATE SET
                    id = excluded.id,
                    status = 'ACTIVE',
                    source_application_id = 'MANUAL',
                    approved_by = excluded.approved_by,
                    approved_at = excluded.approved_at,
                    expires_at = excluded.expires_at,
                    last_verified_level = excluded.last_verified_level,
                    last_verified_at = excluded.last_verified_at,
                    suspend_reason = NULL,
                    revoked_by = NULL,
                    revoked_at = NULL,
                    grant_type = excluded.grant_type,
                    is_level_exempt = excluded.is_level_exempt
                """
                await self.db.execute(
                    sql,
                    (
                        grant_id,
                        b_id,
                        g_id,
                        u_id,
                        str(actor_id).strip(),
                        now,
                        exp_at,
                        actual_level,
                        now,
                        grant_type,
                        1 if is_level_exempt else 0,
                    ),
                )
                await self.audit.log(
                    actor_id=str(actor_id),
                    actor_type=actor_role,
                    bot_self_id=b_id,
                    group_id=g_id,
                    target_user_id=u_id,
                    action="MANUAL_GRANT",
                    old_state=None,
                    new_state="ACTIVE",
                    details={"grant_id": grant_id, "duration_days": duration_days, "note": note},
                )
        return True, f"已手动向用户 {u_id} 授予群 {g_id} 的 Bot 使用权限（有效期：{'永久' if duration_days == 0 else f'{duration_days}天'}）"

    async def ban_user(
        self,
        actor_id: str,
        actor_role: str,
        bot_self_id: str,
        group_id: str,
        user_id: str,
        reason: str = "",
        duration_days: int = 0,
    ) -> tuple[bool, str]:
        """Add user to blacklist and revoke existing grant."""
        b_id = str(bot_self_id).strip()
        g_id = str(group_id).strip()
        u_id = str(user_id).strip()
        now = int(time.time())

        exp_at = (now + duration_days * 86400) if duration_days > 0 else 0

        async with self._action_lock:
            sql = """
            INSERT INTO blacklist (bot_self_id, group_id, user_id, reason, created_by, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(bot_self_id, group_id, user_id) DO UPDATE SET
                reason = excluded.reason,
                created_by = excluded.created_by,
                created_at = excluded.created_at,
                expires_at = excluded.expires_at
            """
            await self.db.execute(sql, (b_id, g_id, u_id, reason, str(actor_id).strip(), now, exp_at))

            # Revoke grant if active
            await self.db.execute(
                "UPDATE grants SET status = 'REVOKED', revoked_by = ?, revoked_at = ?, suspend_reason = 'BLACKLISTED' WHERE bot_self_id = ? AND group_id = ? AND user_id = ?",
                (str(actor_id).strip(), now, b_id, g_id, u_id),
            )
            await self.db.commit()

        await self.audit.log(
            actor_id=str(actor_id),
            actor_type=actor_role,
            bot_self_id=b_id,
            group_id=g_id,
            target_user_id=u_id,
            action="USER_BANNED",
            new_state="BLACKLISTED",
            details={"reason": reason, "duration_days": duration_days},
        )
        return True, f"已将用户 {u_id} 加入群 {g_id} 黑名单"

    async def unban_user(
        self,
        actor_id: str,
        actor_role: str,
        bot_self_id: str,
        group_id: str,
        user_id: str,
    ) -> tuple[bool, str]:
        """Remove user from blacklist. Separate group unban from global unban."""
        b_id = str(bot_self_id).strip()
        g_id = str(group_id).strip()
        u_id = str(user_id).strip()

        async with self._action_lock:
            if g_id.upper() == "ALL":
                cursor = await self.db.execute(
                    "DELETE FROM blacklist WHERE bot_self_id = ? AND group_id = 'ALL' AND user_id = ?",
                    (b_id, u_id),
                )
            else:
                cursor = await self.db.execute(
                    "DELETE FROM blacklist WHERE bot_self_id = ? AND group_id = ? AND user_id = ?",
                    (b_id, g_id, u_id),
                )
            await self.db.commit()

        scope_desc = "全局" if g_id.upper() == "ALL" else f"群 {g_id}"
        if cursor.rowcount == 0:
            return False, f"用户 {u_id} 不在{scope_desc}的黑名单中"

        await self.audit.log(
            actor_id=str(actor_id),
            actor_type=actor_role,
            bot_self_id=b_id,
            group_id=g_id,
            target_user_id=u_id,
            action="USER_UNBANNED",
            old_state="BLACKLISTED",
            new_state=None,
        )
        return True, f"已将用户 {u_id} 从{scope_desc}黑名单中移除"

    async def check_and_increment_rate_limit(
        self, key: str, max_count: int, window_seconds: int = 3600
    ) -> bool:
        """Check sliding window rate limit. Returns True if allowed and increments, False if exceeded."""
        now = int(time.time())
        row = await self.db.fetchone(
            "SELECT count, window_start FROM rate_limits WHERE key = ?",
            (key,),
        )
        if row is None or (now - int(row["window_start"])) >= window_seconds:
            await self.db.execute(
                """
                INSERT INTO rate_limits (key, count, window_start) VALUES (?, 1, ?)
                ON CONFLICT(key) DO UPDATE SET count = 1, window_start = excluded.window_start
                """,
                (key, now),
            )
            await self.db.commit()
            return True

        if int(row["count"]) >= max_count:
            return False

        await self.db.execute(
            "UPDATE rate_limits SET count = count + 1 WHERE key = ?",
            (key,),
        )
        await self.db.commit()
        return True

    async def add_reviewer(self, qq_id: str, added_by: str = "") -> tuple[bool, str]:
        """Persist a reviewer to SQLite and memory."""
        u_id = str(qq_id).strip()
        now = int(time.time())
        if u_id not in self.config.reviewer_qq_ids:
            self.config.reviewer_qq_ids.append(u_id)

        await self.db.execute(
            """
            INSERT INTO reviewers (qq_id, added_by, added_at) VALUES (?, ?, ?)
            ON CONFLICT(qq_id) DO UPDATE SET added_by = excluded.added_by, added_at = excluded.added_at
            """,
            (u_id, str(added_by).strip(), now),
        )
        await self.db.commit()
        return True, f"已成功将 QQ {u_id} 添加为审核员"

    async def remove_reviewer(self, qq_id: str) -> tuple[bool, str]:
        """Remove a reviewer from SQLite and memory."""
        u_id = str(qq_id).strip()
        if u_id in self.config.reviewer_qq_ids:
            self.config.reviewer_qq_ids.remove(u_id)

        await self.db.execute("DELETE FROM reviewers WHERE qq_id = ?", (u_id,))
        await self.db.commit()
        return True, f"已成功将 QQ {u_id} 从审核员名单中移除"

    async def is_reviewer(self, user_id: str) -> bool:
        """Check if user is a reviewer or owner."""
        u_id = str(user_id).strip()
        if self.config.is_owner(u_id):
            return True
        if self.config.is_reviewer(u_id):
            return True
        row = await self.db.fetchone("SELECT qq_id FROM reviewers WHERE qq_id = ?", (u_id,))
        return row is not None

    async def list_reviewers(self) -> list[str]:
        """List all reviewer QQ IDs."""
        rows = await self.db.fetchall("SELECT qq_id FROM reviewers")
        db_reviewers = [r["qq_id"] for r in rows]
        combined = list(set(self.config.reviewer_qq_ids + db_reviewers))
        return combined

    async def sync_reviewers_from_db(self) -> None:
        """Load reviewers from database into config on startup."""
        try:
            rows = await self.db.fetchall("SELECT qq_id FROM reviewers")
            for r in rows:
                qid = str(r["qq_id"]).strip()
                if qid and qid not in self.config.reviewer_qq_ids:
                    self.config.reviewer_qq_ids.append(qid)
        except Exception as e:
            logger.warning(f"Could not sync reviewers from db: {e}")

    async def clean_expired(self) -> tuple[int, int]:
        """Expire stale applications and expired grants (expires_at <= now)."""
        now = int(time.time())
        # Expire pending applications past expires_at
        c1 = await self.db.execute(
            "UPDATE applications SET status = 'EXPIRED' WHERE status = 'PENDING' AND expires_at > 0 AND expires_at <= ?",
            (now,),
        )
        # Expire active grants past expires_at
        c2 = await self.db.execute(
            "UPDATE grants SET status = 'EXPIRED' WHERE status = 'ACTIVE' AND expires_at > 0 AND expires_at <= ?",
            (now,),
        )
        await self.db.commit()
        return c1.rowcount, c2.rowcount
