"""Command execution handlers for user applications and administrative management."""

from __future__ import annotations

import logging
import time
from typing import Any

from .approval_service import ApprovalService
from .audit_service import AuditService
from .config import PluginConfig
from .database import Database
from .models import GrantStatus

logger = logging.getLogger("astrbot_plugin_group_approval.commands")


class CommandDispatcher:
    """Dispatches and executes user and administrative commands."""

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

    # ---------------- USER COMMANDS ----------------

    async def handle_apply(
        self,
        bot_self_id: str,
        group_id: str,
        user_id: str,
        reason: str = "",
    ) -> str:
        """Handle /申请使用."""
        if not group_id:
            return "「/申请使用」指令必须在目标 QQ 群内发送。"

        success, msg, app = await self.approval.apply_for_access(
            bot_self_id=bot_self_id,
            group_id=group_id,
            user_id=user_id,
            reason=reason,
        )
        return msg

    async def handle_my_status(
        self,
        bot_self_id: str,
        group_id: str,
        user_id: str,
    ) -> str:
        """Handle /我的权限."""
        if not group_id:
            return "「/我的权限」指令必须在目标 QQ 群内发送。"

        policy = await self.approval.get_group_policy(bot_self_id, group_id)
        if not policy.enabled:
            return f"群 {group_id} 尚未开启 Bot 使用准入机制。"

        grant = await self.approval.get_active_grant(bot_self_id, group_id, user_id)
        if grant is None:
            # Check if pending application exists
            pending = await self.approval.get_pending_application(bot_self_id, group_id, user_id)
            if pending:
                return (
                    f"【当前权限状态：未授权（审核中）】\n"
                    f"申请编号：{pending.id}\n"
                    f"提交等级：LV{pending.level_at_application}\n"
                    f"提交时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(pending.created_at))}\n"
                    f"状态：等待管理员审批"
                )
            return (
                f"【当前权限状态：未授权】\n"
                f"本群准入门槛：LV{policy.min_level}\n"
                f"您尚未获得当前群的使用权限。达到群等级后，可发送「/申请使用」提交申请。"
            )

        exp_desc = "永久有效" if grant.expires_at == 0 else time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(grant.expires_at))

        status_zh = {
            GrantStatus.ACTIVE: "正常使用中",
            GrantStatus.SUSPENDED: f"已暂停（{grant.suspend_reason or '等级未达标'}）",
            GrantStatus.REVOKED: f"已撤销（{grant.suspend_reason or '管理员撤销'}）",
            GrantStatus.EXPIRED: "已过期",
        }.get(grant.status, grant.status.value)

        return (
            f"【当前权限状态：{status_zh}】\n"
            f"群号：{group_id}\n"
            f"QQ：{user_id}\n"
            f"授权状态：{grant.status.value}\n"
            f"最后核验等级：LV{grant.last_verified_level}\n"
            f"本群门槛：LV{policy.min_level}\n"
            f"授权有效期：{exp_desc}\n"
            f"批准人：{grant.approved_by or '系统'}"
        )

    async def handle_application_status(
        self,
        bot_self_id: str,
        group_id: str,
        user_id: str,
    ) -> str:
        """Handle /申请状态."""
        if not group_id:
            return "「/申请状态」指令必须在目标 QQ 群内发送。"

        pending = await self.approval.get_pending_application(bot_self_id, group_id, user_id)
        if pending is None:
            # Look for most recent non-pending application
            recent = await self.db.fetchone(
                """
                SELECT id, status, level_at_application, created_at, reviewed_at, review_note
                FROM applications
                WHERE bot_self_id = ? AND group_id = ? AND user_id = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (bot_self_id, group_id, user_id),
            )
            if recent:
                status_name = {
                    "APPROVED": "已批准",
                    "REJECTED": f"已拒绝（原因：{recent['review_note'] or '未说明'}）",
                    "CANCELLED": "已主动取消",
                    "EXPIRED": "已过期",
                }.get(recent["status"], recent["status"])
                return (
                    f"【最近申请记录】\n"
                    f"申请编号：{recent['id']}\n"
                    f"状态：{status_name}\n"
                    f"提交时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(recent['created_at']))}"
                )
            return "您在当前群暂无任何申请记录。达到群等级要求后可发送「/申请使用」申请。"

        exp_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(pending.expires_at))
        return (
            f"【待审核申请信息】\n"
            f"申请编号：{pending.id}\n"
            f"群号：{group_id}\n"
            f"申请时等级：LV{pending.level_at_application}\n"
            f"提交时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(pending.created_at))}\n"
            f"有效期至：{exp_str}\n"
            f"状态：等待管理员审批"
        )

    async def handle_cancel_application(
        self,
        bot_self_id: str,
        group_id: str,
        user_id: str,
    ) -> str:
        """Handle /取消申请."""
        if not group_id:
            return "「/取消申请」指令必须在目标 QQ 群内发送。"

        success, msg = await self.approval.cancel_application(user_id, bot_self_id, group_id)
        return msg

    async def handle_rules(
        self,
        bot_self_id: str,
        group_id: str,
    ) -> str:
        """Handle /使用规则."""
        if not group_id:
            return "「/使用规则」指令必须在目标 QQ 群内发送。"

        policy = await self.approval.get_group_policy(bot_self_id, group_id)
        dur_desc = "永久授权" if policy.grant_duration_seconds == 0 else f"{policy.grant_duration_seconds // 86400} 天"

        return (
            f"【群 {group_id} Bot 使用准入规则】\n"
            f"• 状态：{'已开启准入' if policy.enabled else '未开启准入'}\n"
            f"• 最低申请群等级：LV{policy.min_level}\n"
            f"• 管理员审批：{'必须审批' if policy.approval_required else '免审批自动授权'}\n"
            f"• 授权有效期：{dur_desc}\n"
            f"• 等级复核周期：{policy.recheck_interval_seconds} 秒\n"
            f"• 资格恢复策略：{'等级恢复后自动激活' if policy.restore_policy == 'automatic' else '需重新人工申请'}\n"
            f"• 群主与群管豁免：{'豁免' if policy.group_admin_bypass else '需同等申请'}\n\n"
            f"符合条件的成员可发送「/申请使用」提交权限申请。"
        )

    # ---------------- ADMIN COMMANDS (/ga ...) ----------------

    async def handle_admin_command(
        self,
        bot_self_id: str,
        sender_id: str,
        group_id: str | None,
        args: list[str],
    ) -> str:
        """Parse and execute administrative commands."""
        if not args:
            return (
                "【Group Approval 管理指令】\n"
                "• /ga pending [群号] - 查看待审核申请\n"
                "• /ga approve <申请编号> [备注] - 批准申请\n"
                "• /ga reject <申请编号> [原因] - 拒绝申请\n"
                "• /ga revoke <群号> <QQ> [原因] - 撤销权限\n"
                "• /ga user <群号> <QQ> - 查询用户授权\n"
                "• /ga list <群号> - 获批用户列表\n"
                "• /ga history <申请编号> - 申请处理历史\n\n"
                "Owner 专属指令：\n"
                "• /ga grant <群号> <QQ> [天数] - 手动授权\n"
                "• /ga ban <群号> <QQ> [原因] [天数] - 拉黑用户\n"
                "• /ga unban <群号> <QQ> - 解除黑名单\n"
                "• /ga reviewer add/remove <QQ> - 审核员管理\n"
                "• /ga level <群号> <数字> - 修改门槛等级\n"
                "• /ga enable/disable <群号> - 开关群准入\n"
                "• /ga status - 系统运行概况"
            )

        subcmd = args[0].lower()
        subargs = args[1:]

        is_owner = self.config.is_owner(sender_id)
        is_reviewer = await self.approval.is_reviewer(sender_id)

        if not is_reviewer:
            return "权限不足：您不是 Group Approval 审核员或 Owner。"

        actor_role = "OWNER" if is_owner else "REVIEWER"

        # Reviewer & Owner Commands
        if subcmd == "pending":
            target_gid = subargs[0] if subargs else group_id
            return await self._cmd_pending(bot_self_id, target_gid)

        elif subcmd == "approve":
            if not subargs:
                return "用法：/ga approve <申请编号> [备注]"
            app_id = subargs[0]
            note = " ".join(subargs[1:]) if len(subargs) > 1 else "管理员批准"
            success, msg = await self.approval.approve_application(
                actor_id=sender_id,
                actor_role=actor_role,
                application_id=app_id,
                note=note,
            )
            return msg

        elif subcmd == "reject":
            if not subargs:
                return "用法：/ga reject <申请编号> [原因]"
            app_id = subargs[0]
            reason = " ".join(subargs[1:]) if len(subargs) > 1 else "不符合本群准入要求"
            success, msg = await self.approval.reject_application(
                actor_id=sender_id,
                actor_role=actor_role,
                application_id=app_id,
                reason=reason,
            )
            return msg

        elif subcmd == "revoke":
            if len(subargs) < 2:
                return "用法：/ga revoke <群号> <QQ> [原因]"
            t_gid = subargs[0]
            t_uid = subargs[1]
            reason = " ".join(subargs[2:]) if len(subargs) > 2 else "管理员撤销授权"
            success, msg = await self.approval.revoke_grant(
                actor_id=sender_id,
                actor_role=actor_role,
                bot_self_id=bot_self_id,
                group_id=t_gid,
                user_id=t_uid,
                reason=reason,
            )
            return msg

        elif subcmd == "user":
            if len(subargs) < 2:
                return "用法：/ga user <群号> <QQ>"
            return await self._cmd_user(bot_self_id, subargs[0], subargs[1])

        elif subcmd == "list":
            t_gid = subargs[0] if subargs else group_id
            if not t_gid:
                return "请指定群号：/ga list <群号>"
            return await self._cmd_list(bot_self_id, t_gid)

        elif subcmd == "history":
            if not subargs:
                return "用法：/ga history <申请编号>"
            return await self._cmd_history(subargs[0])

        # Owner Exclusive Commands
        if not is_owner:
            return f"权限不足：指令「/ga {subcmd}」仅限插件 Owner 执行。"

        if subcmd == "grant":
            if len(subargs) < 2:
                return "用法：/ga grant <群号> <QQ> [天数, 默认0永久] [--exempt]"
            t_gid = subargs[0]
            t_uid = subargs[1]
            days = 0
            is_exempt = False
            for extra in subargs[2:]:
                if extra.isdigit():
                    days = int(extra)
                elif extra.lower() in ("--exempt", "-e", "exempt", "免等级"):
                    is_exempt = True
            success, msg = await self.approval.grant_manually(
                actor_id=sender_id,
                actor_role="OWNER",
                bot_self_id=bot_self_id,
                group_id=t_gid,
                user_id=t_uid,
                duration_days=days,
                note="Owner手动授权" + (" (免等级检查)" if is_exempt else ""),
                is_level_exempt=is_exempt,
            )
            return msg

        elif subcmd == "ban":
            if len(subargs) < 2:
                return "用法：/ga ban <群号> <QQ> [原因] [天数]"
            t_gid = subargs[0]
            t_uid = subargs[1]
            reason = subargs[2] if len(subargs) > 2 else "违反群规"
            days = int(subargs[3]) if len(subargs) > 3 and subargs[3].isdigit() else 0
            success, msg = await self.approval.ban_user(
                actor_id=sender_id,
                actor_role="OWNER",
                bot_self_id=bot_self_id,
                group_id=t_gid,
                user_id=t_uid,
                reason=reason,
                duration_days=days,
            )
            return msg

        elif subcmd == "unban":
            if len(subargs) < 2:
                return "用法：/ga unban <群号|all> <QQ>"
            t_gid = subargs[0]
            t_uid = subargs[1]
            success, msg = await self.approval.unban_user(
                actor_id=sender_id,
                actor_role="OWNER",
                bot_self_id=bot_self_id,
                group_id=t_gid,
                user_id=t_uid,
            )
            return msg

        elif subcmd == "reviewer":
            if not subargs:
                return "用法：/ga reviewer add <QQ>、/ga reviewer remove <QQ> 或 /ga reviewer list"
            action = subargs[0].lower()
            if action == "list":
                reviewers = await self.approval.list_reviewers()
                if not reviewers:
                    return "当前未配置任何审核员。"
                return f"【审核员列表（共 {len(reviewers)} 人）】\n" + "\n".join(f"• QQ: {q}" for q in reviewers)
            if len(subargs) < 2:
                return "用法：/ga reviewer add <QQ> 或 /ga reviewer remove <QQ>"
            t_uid = subargs[1]
            if action == "add":
                ok, msg = await self.approval.add_reviewer(t_uid, added_by=sender_id)
                return msg
            elif action == "remove":
                ok, msg = await self.approval.remove_reviewer(t_uid)
                return msg
            return "未知子命令，仅支持 add、remove 或 list。"

        elif subcmd == "level":
            if len(subargs) < 2 or not subargs[1].isdigit():
                return "用法：/ga level <群号> <最低等级数值>"
            t_gid = subargs[0]
            new_lvl = int(subargs[1])
            pol = await self.approval.get_group_policy(bot_self_id, t_gid)
            pol.min_level = new_lvl
            await self.approval.save_group_policy(pol)
            return f"群 {t_gid} 申请最低门槛已调整为 LV{new_lvl}。"

        elif subcmd == "enable":
            if not subargs:
                return "用法：/ga enable <群号>"
            t_gid = subargs[0]
            pol = await self.approval.get_group_policy(bot_self_id, t_gid)
            pol.enabled = True
            await self.approval.save_group_policy(pol)
            return f"已启用群 {t_gid} 的准入鉴权。"

        elif subcmd == "disable":
            if not subargs:
                return "用法：/ga disable <群号>"
            t_gid = subargs[0]
            pol = await self.approval.get_group_policy(bot_self_id, t_gid)
            pol.enabled = False
            await self.approval.save_group_policy(pol)
            return f"已禁用群 {t_gid} 的准入鉴权（普通成员将无法调用 Bot）。"

        elif subcmd == "status":
            return await self._cmd_status(bot_self_id)

        return f"未知管理指令：{subcmd}。发送 /ga 查看帮助。"

    async def _cmd_pending(self, bot_self_id: str, group_id: str | None) -> str:
        sql = "SELECT id, group_id, user_id, level_at_application, created_at FROM applications WHERE status = 'PENDING'"
        params: list[Any] = []
        if group_id:
            sql += " AND group_id = ?"
            params.append(group_id)
        sql += " ORDER BY created_at ASC LIMIT 10"

        rows = await self.db.fetchall(sql, tuple(params))
        if not rows:
            return "当前暂无待审核申请。"

        lines = [f"【待审核申请列表（共 {len(rows)} 条）】"]
        for r in rows:
            t_str = time.strftime("%m-%d %H:%M", time.localtime(r["created_at"]))
            lines.append(
                f"• [{r['id']}] 群:{r['group_id']} QQ:{r['user_id']} LV{r['level_at_application']} ({t_str})"
            )
        lines.append("\n操作提示：使用 /ga approve <编号> 或 /ga reject <编号>")
        return "\n".join(lines)

    async def _cmd_user(self, bot_self_id: str, group_id: str, user_id: str) -> str:
        grant = await self.approval.get_active_grant(bot_self_id, group_id, user_id)
        banned, ban_reason = await self.approval.is_blacklisted(bot_self_id, group_id, user_id)

        lines = [f"【用户权限档案：{user_id} @ 群 {group_id}】"]
        if banned:
            lines.append(f"• 黑名单状态：已拉黑（{ban_reason}）")

        if grant is None:
            lines.append("• 授权状态：无授权记录")
        else:
            exp_str = "永久有效" if grant.expires_at == 0 else time.strftime("%Y-%m-%d", time.localtime(grant.expires_at))
            lines.extend(
                [
                    f"• 授权状态：{grant.status.value}",
                    f"• 批准人：{grant.approved_by}",
                    f"• 最后核验等级：LV{grant.last_verified_level}",
                    f"• 有效期至：{exp_str}",
                ]
            )

        # Recent applications
        apps = await self.db.fetchall(
            "SELECT id, status, level_at_application, created_at FROM applications WHERE group_id = ? AND user_id = ? ORDER BY created_at DESC LIMIT 3",
            (group_id, user_id),
        )
        if apps:
            lines.append("\n最近申请：")
            for a in apps:
                lines.append(f"• [{a['id']}] 状态:{a['status']} (LV{a['level_at_application']})")

        return "\n".join(lines)

    async def _cmd_list(self, bot_self_id: str, group_id: str) -> str:
        rows = await self.db.fetchall(
            "SELECT user_id, status, last_verified_level, expires_at FROM grants WHERE group_id = ? AND status = 'ACTIVE' LIMIT 20",
            (group_id,),
        )
        if not rows:
            return f"群 {group_id} 暂无已激活的授权用户。"

        lines = [f"【群 {group_id} 获批用户列表（前 {len(rows)} 名）】"]
        for r in rows:
            lines.append(f"• QQ: {r['user_id']} | LV{r['last_verified_level']}")
        return "\n".join(lines)

    async def _cmd_history(self, app_id: str) -> str:
        row = await self.db.fetchone("SELECT * FROM applications WHERE id = ?", (app_id,))
        if row is None:
            return f"未找到申请编号 {app_id}。"

        return (
            f"【申请单 {app_id} 详情】\n"
            f"群号：{row['group_id']}\n"
            f"申请人QQ：{row['user_id']}\n"
            f"申请等级：LV{row['level_at_application']}\n"
            f"当前状态：{row['status']}\n"
            f"申请时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(row['created_at']))}\n"
            f"审核时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(row['reviewed_at'])) if row['reviewed_at'] else '未审核'}\n"
            f"审核人：{row['reviewer_id'] or '无'}\n"
            f"审核备注：{row['review_note'] or '无'}"
        )

    async def _cmd_status(self, bot_self_id: str) -> str:
        groups_cnt = (await self.db.fetchone("SELECT COUNT(*) FROM group_policies WHERE enabled = 1"))[0]
        pending_cnt = (await self.db.fetchone("SELECT COUNT(*) FROM applications WHERE status = 'PENDING'"))[0]
        grants_cnt = (await self.db.fetchone("SELECT COUNT(*) FROM grants WHERE status = 'ACTIVE'"))[0]
        suspended_cnt = (await self.db.fetchone("SELECT COUNT(*) FROM grants WHERE status = 'SUSPENDED'"))[0]
        blacklist_cnt = (await self.db.fetchone("SELECT COUNT(*) FROM blacklist"))[0]

        return (
            "【Group Approval 插件运行状态】\n"
            f"• 插件状态：运行中 (Enabled)\n"
            f"• 受管启用群数：{groups_cnt}\n"
            f"• 待审核申请数：{pending_cnt}\n"
            f"• 已授权活跃用户：{grants_cnt}\n"
            f"• 暂停授权用户：{suspended_cnt}\n"
            f"• 黑名单人数：{blacklist_cnt}\n"
            f"• Owner 人数：{len(self.config.owner_qq_ids)}\n"
            f"• 审核员人数：{len(self.config.reviewer_qq_ids)}"
        )
