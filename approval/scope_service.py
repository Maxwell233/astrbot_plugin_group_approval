"""Bot-wide GROUP/PRIVATE entitlements. Network checks never hold a SQLite writer lock."""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass
from typing import Literal

from .approval_service import ApprovalService
from .audit_service import AuditService
from .config import PluginConfig
from .database import Database
from .level_service import LevelService
from .notification_service import NotificationService

Scope = Literal["GROUP", "PRIVATE"]
SCOPES = ("GROUP", "PRIVATE")
LABELS = {"GROUP": "群聊", "PRIVATE": "私聊"}
PRIMARY_CHANGED = "申请所属主群已变更，请用户重新提交申请。"


class ScopeError(ValueError):
    """Expected user-facing admission or management error."""


def parse_scope(value: str) -> Scope:
    text = str(value).strip().upper()
    if text in {"GROUP", "群聊"}:
        return "GROUP"
    if text in {"PRIVATE", "私聊"}:
        return "PRIVATE"
    raise ScopeError("请指定 group（群聊）或 private（私聊）。")


def valid_qq(value: str) -> bool:
    return bool(value and value.isascii() and value.isdecimal() and 0 < len(value) <= 20 and value[0] != "0")


@dataclass(frozen=True)
class Settings:
    bot_self_id: str
    primary_group_id: str = ""
    primary_revision: int = 0
    revision: int = 0
    cache_ttl: int = 300
    group_to_private: bool = False
    private_to_group: bool = False
    owner_bypass: bool = False


@dataclass(frozen=True)
class Policy:
    min_level: int
    approval_required: bool = True


class ScopeService:
    def __init__(
        self, db: Database, level: LevelService, audit: AuditService,
        notifications: NotificationService, config: PluginConfig, legacy: ApprovalService,
    ):
        self.db = db
        self.level = level
        self.audit = audit
        self.notifications = notifications
        self.config = config
        self.legacy = legacy

    async def settings(self, bot: str) -> Settings:
        row = await self.db.fetchone("SELECT * FROM bot_settings WHERE bot_self_id = ?", (bot,))
        return Settings(**dict(row)) if row else Settings(bot)

    async def policy(self, bot: str, scope: Scope) -> Policy:
        row = await self.db.fetchone(
            "SELECT min_level, approval_required FROM scope_policies WHERE bot_self_id = ? AND scope = ?",
            (bot, scope),
        )
        return Policy(**dict(row)) if row else Policy(10 if scope == "GROUP" else 20)

    async def grant(self, bot: str, user: str, scope: Scope):
        return await self.db.fetchone(
            "SELECT * FROM grants_v2 WHERE bot_self_id = ? AND user_id = ? AND scope = ?", (bot, user, scope)
        )

    async def application(self, bot: str, app_id: str):
        return await self.db.fetchone(
            "SELECT * FROM applications_v2 WHERE bot_self_id = ? AND id = ?", (bot, app_id)
        )

    async def _restricted(self, bot: str, user: str, scope: Scope, *, owner_override: bool = False) -> bool:
        row = await self.db.fetchone(
            """SELECT 1 FROM grant_restrictions WHERE bot_self_id = ? AND user_id = ? AND scope = ?
               AND (kind = 'BLACKLIST' OR ? = 0) LIMIT 1""",
            (bot, user, scope, owner_override),
        )
        return row is not None

    async def _log(self, bot, actor, role, action, *, user="", scope="", primary="", app_id=None, **details):
        await self.audit.log(
            actor_id=actor, actor_type=role, bot_self_id=bot, group_id=primary,
            target_user_id=user, action=action, request_id=app_id, details={"scope": scope, **details},
        )

    async def _ensure_current(self, settings: Settings) -> None:
        if (await self.settings(settings.bot_self_id)).revision != settings.revision:
            raise ScopeError("Bot 的主群或准入规则已变更，请重试。")

    async def configure(
        self, bot: str, actor: str, *, primary_group_id: str | None = None,
        policies: dict[Scope, dict] | None = None, **options,
    ) -> Settings:
        """Called only after the command/HTTP boundary authorizes an Owner."""
        before = await self.settings(bot)
        changes = dict(options)
        primary_changed = primary_group_id is not None and primary_group_id != before.primary_group_id
        if primary_changed:
            if not valid_qq(primary_group_id):
                raise ScopeError("主群号格式无效。")
            probe = await self.level.get_group_member_level(bot, primary_group_id, bot, force_refresh=True)
            if not probe.success or not probe.is_member:
                raise ScopeError("无法确认 Bot 已加入新主群且可查询成员等级。")
            changes["primary_group_id"] = primary_group_id
            changes["primary_revision"] = before.primary_revision + 1

        fields = {"primary_group_id", "primary_revision", "cache_ttl",
                  "group_to_private", "private_to_group", "owner_bypass"}
        if changes.keys() - fields:
            raise ScopeError("不支持的配置。")
        async with self.db.transaction():
            await self._ensure_current(before)
            await self.db.execute("INSERT OR IGNORE INTO bot_settings (bot_self_id) VALUES (?)", (bot,))
            for scope, updates in (policies or {}).items():
                current = asdict(await self.policy(bot, scope))
                current.update(updates)
                await self.db.execute(
                    """INSERT INTO scope_policies VALUES (?, ?, ?, ?)
                       ON CONFLICT(bot_self_id, scope) DO UPDATE SET
                         min_level = excluded.min_level, approval_required = excluded.approval_required""",
                    (bot, scope, current["min_level"], current["approval_required"]),
                )
            assignments = ", ".join(f"{key} = ?" for key in changes)
            await self.db.execute(
                "UPDATE bot_settings SET revision = revision + 1"
                + (", " + assignments if assignments else "") + " WHERE bot_self_id = ?",
                (*changes.values(), bot),
            )
            if primary_changed:
                await self.db.execute("DELETE FROM primary_group_levels WHERE bot_self_id = ?", (bot,))
                cancelled = await self.db.fetchall(
                    """UPDATE applications_v2 SET status = 'CANCELLED', reviewed_at = ?, reviewer_id = ?,
                       review_note = ? WHERE bot_self_id = ? AND status = 'PENDING'
                         AND (primary_revision != ? OR primary_group_id != ?)
                       RETURNING id, user_id, scope""",
                    (int(time.time()), actor, PRIMARY_CHANGED, bot, changes["primary_revision"], primary_group_id),
                )
                for app in cancelled:
                    await self._log(
                        bot, actor, "OWNER", "LLM_APPLICATION_CANCELLED", user=app["user_id"],
                        scope=app["scope"], primary=before.primary_group_id, app_id=app["id"],
                        reason=PRIMARY_CHANGED,
                    )
            await self._log(
                bot, actor, "OWNER", "LLM_SETTINGS_UPDATED",
                primary=primary_group_id or before.primary_group_id, previous_primary=before.primary_group_id,
                changes=changes, policies=policies or {},
            )
        return await self.settings(bot)

    async def _member_level(self, settings: Settings, user: str, *, force: bool) -> int | None:
        bot, group = settings.bot_self_id, settings.primary_group_id
        if not group:
            raise ScopeError("此 Bot 尚未配置主群。")
        row = await self.db.fetchone(
            """SELECT level, verified_at, lookup_epoch FROM primary_group_levels
               WHERE bot_self_id = ? AND primary_group_id = ? AND user_id = ? AND primary_revision = ?""",
            (bot, group, user, settings.primary_revision),
        )
        epoch = row["lookup_epoch"] if row else 0
        if (not force and settings.cache_ttl and row
                and row["verified_at"] > int(time.time()) - settings.cache_ttl):
            return row["level"]
        result = await self.level.get_group_member_level(
            bot, group, user, force_refresh=True, lookup_generation=(settings.primary_revision, epoch),
        )
        if not result.success or not result.is_member:
            await self.db.execute(
                """UPDATE primary_group_levels SET verified_at = 0 WHERE bot_self_id = ? AND primary_group_id = ?
                   AND user_id = ? AND primary_revision = ? AND lookup_epoch = ?""",
                (bot, group, user, settings.primary_revision, epoch),
            )
            if result.error_code == "NOT_MEMBER":
                return None
            raise ScopeError("暂时无法核验主群成员身份或等级，请稍后再试。")

        written = await self.db.execute(
            """INSERT INTO primary_group_levels
                   (bot_self_id, primary_group_id, user_id, primary_revision, level, verified_at, lookup_epoch)
               SELECT bot_self_id, primary_group_id, ?, primary_revision, ?, ?, ? FROM bot_settings
               WHERE bot_self_id = ? AND primary_group_id = ? AND primary_revision = ?
               ON CONFLICT(bot_self_id, primary_group_id, user_id) DO UPDATE SET
                 primary_revision = excluded.primary_revision, level = excluded.level, verified_at = excluded.verified_at
               WHERE primary_group_levels.lookup_epoch = excluded.lookup_epoch
                 AND primary_group_levels.verified_at <= excluded.verified_at""",
            (user, result.level, result.checked_at, epoch, bot, group, settings.primary_revision),
        )
        if written.rowcount != 1:
            raise ScopeError("Bot 的主群或成员核验状态已变更，请重试。")
        return result.level

    @staticmethod
    def _qualification(level: int | None, policy: Policy, grant=None) -> str | None:
        level_exempt = bool(grant and grant["level_exempt"])
        membership_exempt = bool(grant and grant["membership_exempt"])
        if level is None:
            if level_exempt and membership_exempt:
                return None
            return "需要有效的主群成员身份和等级。"
        if not level_exempt and level < policy.min_level:
            return f"主群等级 LV{level}，需要 LV{policy.min_level}。"
        return None

    async def check_chat(self, bot: str, user: str, scope: Scope) -> str | None:
        """Return the denial notice, or None. This path never grants access."""
        settings = await self.settings(bot)
        if await self._restricted(bot, user, scope):
            return "此域的 AI 聊天权限已受限制。"
        if settings.owner_bypass and self.config.is_owner(user):
            async with self.db.transaction():
                current = await self.settings(bot)
                if current.owner_bypass and not await self._restricted(bot, user, scope):
                    return None
        grant = await self.grant(bot, user, scope)
        if not grant or grant["status"] != "ACTIVE":
            return f"尚无可用的{LABELS[scope]} AI 权限，请发送 /申请{LABELS[scope]}。"
        policy = await self.policy(bot, scope)
        try:
            level = None
            if not (grant["level_exempt"] and grant["membership_exempt"]):
                level = await self._member_level(settings, user, force=False)
            async with self.db.transaction():
                await self._ensure_current(settings)
                current = await self.grant(bot, user, scope)
                if (not current or current["revision"] != grant["revision"]
                        or await self._restricted(bot, user, scope)):
                    return "AI 权限状态已变更，请重试。"
                reason = self._qualification(level, policy, current)
                if reason:
                    await self.db.execute(
                        """UPDATE grants_v2 SET status = 'SUSPENDED', revision = revision + 1, updated_at = ?
                           WHERE bot_self_id = ? AND user_id = ? AND scope = ? AND revision = ?""",
                        (int(time.time()), bot, user, scope, grant["revision"]),
                    )
                    await self._log(bot, "SYSTEM", "SYSTEM", "LLM_GRANT_SUSPENDED", user=user,
                                    scope=scope, primary=settings.primary_group_id, reason=reason)
                return reason
        except ScopeError as exc:
            return str(exc)

    async def _write_grant(self, bot, user, scope, source, actor, *, app_id=None, flags=(False, False)):
        await self.db.execute(
            """INSERT INTO grants_v2 (bot_self_id, user_id, scope, status, source, source_application_id,
               level_exempt, membership_exempt, actor_id, updated_at)
               VALUES (?, ?, ?, 'ACTIVE', ?, ?, ?, ?, ?, ?)
               ON CONFLICT(bot_self_id, user_id, scope) DO UPDATE SET status = 'ACTIVE', source = excluded.source,
                 source_application_id = excluded.source_application_id, level_exempt = excluded.level_exempt,
                 membership_exempt = excluded.membership_exempt, actor_id = excluded.actor_id,
                 updated_at = excluded.updated_at, revision = grants_v2.revision + 1""",
            (bot, user, scope, source, app_id, *flags, actor, int(time.time())),
        )

    async def _restore(self, grant, settings, actor, role):
        await self.db.execute(
            """UPDATE grants_v2 SET status = 'ACTIVE', revision = revision + 1, updated_at = ?
               WHERE bot_self_id = ? AND user_id = ? AND scope = ? AND status = 'SUSPENDED' AND revision = ?""",
            (int(time.time()), grant["bot_self_id"], grant["user_id"], grant["scope"], grant["revision"]),
        )
        await self._log(grant["bot_self_id"], actor, role, "LLM_GRANT_RESTORED", user=grant["user_id"],
                        scope=grant["scope"], primary=settings.primary_group_id)

    async def _pending(self, bot, user, scope):
        return await self.db.fetchone(
            """SELECT id FROM applications_v2 WHERE bot_self_id = ? AND user_id = ?
               AND scope = ? AND status = 'PENDING'""", (bot, user, scope)
        )

    async def _cross_source(self, settings, user, scope):
        other = "PRIVATE" if scope == "GROUP" else "GROUP"
        enabled = settings.private_to_group if scope == "GROUP" else settings.group_to_private
        if not enabled:
            return None
        bot = settings.bot_self_id
        source = await self.grant(bot, user, other)
        if source and source["status"] == "ACTIVE" and not await self._restricted(bot, user, other):
            return source
        return None

    async def _application_grant(self, bot, user, scope):
        if await self._restricted(bot, user, scope):
            raise ScopeError("此域已被撤销或列入黑名单，请联系管理员。")
        grant = await self.grant(bot, user, scope)
        if grant and grant["status"] == "REVOKED":
            raise ScopeError("此域已被人工撤销，请联系管理员。")
        return grant

    async def apply(self, bot: str, user: str, scope: Scope, source_group: str, reason: str = "") -> str:
        settings = await self.settings(bot)
        policy = await self.policy(bot, scope)
        grant = await self._application_grant(bot, user, scope)
        if grant and grant["status"] == "ACTIVE":
            return f"已有{LABELS[scope]} AI 聊天权限。"
        existing = await self._pending(bot, user, scope)
        if existing and not grant and policy.approval_required and not await self._cross_source(settings, user, scope):
            return f"已有待审批申请 {existing['id']}。"

        limits = self.config.application
        if not await self.legacy.check_and_increment_rate_limit(
            f"llm-query:{bot}:{user}:{scope}", limits.query_limit, limits.query_window_seconds
        ):
            raise ScopeError("申请核验过于频繁，请稍后重试。")
        level = await self._member_level(settings, user, force=True)
        async with self.db.transaction():
            await self._ensure_current(settings)
            grant = await self._application_grant(bot, user, scope)
            if grant and grant["status"] == "ACTIVE":
                return f"已有{LABELS[scope]} AI 聊天权限。"
            denied = self._qualification(level, policy, grant)
            if denied:
                raise ScopeError(denied)
            if grant and grant["status"] == "SUSPENDED":
                await self._restore(grant, settings, user, "USER")
                return f"{LABELS[scope]} AI 聊天权限已恢复。"
            existing = await self._pending(bot, user, scope)
            source = await self._cross_source(settings, user, scope)
            cross = bool(
                source and not self._qualification(level, await self.policy(bot, source["scope"]), source)
            )
            if cross or not policy.approval_required:
                source_type = "CROSS_GRANT" if cross else "AUTO_NO_APPROVAL"
                app_id = existing["id"] if existing else None
                if app_id:
                    await self.db.execute(
                        """UPDATE applications_v2 SET status = 'APPROVED', reviewed_at = ?,
                           reviewer_id = 'SYSTEM', review_note = ? WHERE id = ? AND status = 'PENDING'""",
                        (int(time.time()), source_type, app_id),
                    )
                await self._write_grant(bot, user, scope, source_type, user, app_id=app_id)
                await self._log(bot, user, "USER", "LLM_GRANT_CREATED", user=user, scope=scope,
                                primary=settings.primary_group_id, source=source_type, app_id=app_id)
                return f"{LABELS[scope]} AI 聊天权限已开通。"
            if existing:
                return f"已有待审批申请 {existing['id']}。"

            limit = self.config.application.rate_limit_per_hour
            if limit > 0 and not await self.legacy.check_and_increment_rate_limit(
                f"llm-apply:{bot}:{user}:{scope}", limit
            ):
                raise ScopeError("申请过于频繁，请稍后重试。")
            app_id = f"L{uuid.uuid4().hex[:12]}"
            await self.db.execute(
                """INSERT INTO applications_v2 (id, bot_self_id, user_id, scope, source_group_id, primary_group_id,
                   primary_revision, level, reason, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)""",
                (app_id, bot, user, scope, source_group, settings.primary_group_id,
                 settings.primary_revision, level, reason, int(time.time())),
            )
            await self._log(bot, user, "USER", "LLM_APPLICATION_SUBMITTED", user=user, scope=scope,
                            primary=settings.primary_group_id, app_id=app_id, source_group_id=source_group)
            reviewers = set(self.config.owner_qq_ids) | set(await self.legacy.list_reviewers())
            for reviewer in sorted(reviewers):
                await self.notifications.enqueue(
                    f"LLM_REVIEW_{app_id}_{reviewer}", reviewer, "private",
                    f"{LABELS[scope]} AI 申请 {app_id}（QQ {user}，主群 LV{level}）。\n"
                    f"私聊 /ga approve {app_id} 或 /ga reject {app_id}。",
                    bot_self_id=bot,
                )
            return f"{LABELS[scope]}申请已提交，编号 {app_id}。"

    @staticmethod
    def _require_application_primary(app, settings: Settings) -> None:
        if (app["primary_revision"] != settings.primary_revision
                or app["primary_group_id"] != settings.primary_group_id):
            raise ScopeError(PRIMARY_CHANGED)

    async def review(self, bot, app_id, actor, role, *, approve: bool, note: str = "") -> str:
        app = await self.application(bot, app_id)
        if not app:
            raise ScopeError("该 Bot 下没有这条待审申请。")
        settings = await self.settings(bot)
        self._require_application_primary(app, settings)
        if app["status"] != "PENDING":
            raise ScopeError("该 Bot 下没有这条待审申请。")
        scope, user = parse_scope(app["scope"]), app["user_id"]
        denied = None
        if approve:
            policy = await self.policy(bot, scope)
            try:
                level = await self._member_level(settings, user, force=True)
            except ScopeError:
                self._require_application_primary(app, await self.settings(bot))
                raise
            denied = self._qualification(level, policy)
        status = "APPROVED" if approve else "REJECTED"
        async with self.db.transaction():
            current = await self.settings(bot)
            self._require_application_primary(app, current)
            if approve:
                if current.revision != settings.revision:
                    raise ScopeError("Bot 的准入规则已变更，请重试。")
                if denied:
                    raise ScopeError(denied)
                existing = await self.grant(bot, user, scope)
                if await self._restricted(bot, user, scope) or (existing and existing["status"] == "REVOKED"):
                    raise ScopeError("该用户受人工撤销或黑名单限制。")
                if existing and existing["status"] == "ACTIVE":
                    raise ScopeError("该用户已有此域授权。")
            changed = await self.db.execute(
                """UPDATE applications_v2 SET status = ?, reviewed_at = ?, reviewer_id = ?, review_note = ?
                   WHERE bot_self_id = ? AND id = ? AND status = 'PENDING'""",
                (status, int(time.time()), actor, note, bot, app_id),
            )
            if changed.rowcount != 1:
                raise ScopeError("此申请已处理。")
            if approve:
                await self._write_grant(bot, user, scope, "MANUAL_APPROVAL", actor, app_id=app_id)
            await self._log(bot, actor, role, f"LLM_APPLICATION_{status}", user=user, scope=scope,
                            primary=settings.primary_group_id, app_id=app_id, note=note)
            label = "已批准" if approve else "已拒绝"
            await self.notifications.enqueue(
                f"LLM_RESULT_{app_id}", user, "private",
                f"你的{LABELS[scope]} AI 申请 {app_id} {label}。{note}", bot_self_id=bot,
            )
        return f"{LABELS[scope]}申请 {app_id} {label}。"

    async def cancel(self, bot, user, scope) -> str:
        async with self.db.transaction():
            row = await self.db.fetchone(
                """SELECT id FROM applications_v2 WHERE bot_self_id = ? AND user_id = ?
                   AND scope = ? AND status = 'PENDING'""", (bot, user, scope)
            )
            if not row:
                raise ScopeError("此域没有待审申请。")
            await self.db.execute("UPDATE applications_v2 SET status = 'CANCELLED' WHERE id = ?", (row["id"],))
            await self._log(bot, user, "USER", "LLM_APPLICATION_CANCELLED", user=user, scope=scope, app_id=row["id"])
        return "已取消申请。"

    async def manual_grant(self, bot, user, scope, actor, *, level_exempt=False, membership_exempt=False):
        settings = await self.settings(bot)
        policy = await self.policy(bot, scope)
        flags = {"level_exempt": level_exempt, "membership_exempt": membership_exempt}
        level = None if all(flags.values()) else await self._member_level(settings, user, force=True)
        denied = self._qualification(level, policy, flags)
        if denied:
            raise ScopeError(denied)
        async with self.db.transaction():
            await self._ensure_current(settings)
            if await self._restricted(bot, user, scope, owner_override=True):
                raise ScopeError("该用户仍在黑名单中。")
            await self._write_grant(bot, user, scope, "OWNER_MANUAL", actor,
                                    flags=(level_exempt, membership_exempt))
            await self.db.execute(
                """DELETE FROM grant_restrictions WHERE bot_self_id = ? AND user_id = ?
                   AND scope = ? AND kind = 'REVOKED'""", (bot, user, scope)
            )
            await self.db.execute(
                """UPDATE applications_v2 SET status = 'CANCELLED', reviewed_at = ?, reviewer_id = ?
                   WHERE bot_self_id = ? AND user_id = ? AND scope = ? AND status = 'PENDING'""",
                (int(time.time()), actor, bot, user, scope),
            )
            await self._log(bot, actor, "OWNER", "LLM_OWNER_GRANT", user=user, scope=scope,
                            primary=settings.primary_group_id, **flags)
        return f"已保存{LABELS[scope]}授权。"

    async def set_exemptions(self, bot, user, scope, actor, *, level_exempt, membership_exempt):
        settings = await self.settings(bot)
        grant = await self.grant(bot, user, scope)
        if not grant or grant["status"] == "REVOKED":
            raise ScopeError("只能修改已有且未撤销授权的豁免。")
        flags = {"level_exempt": level_exempt, "membership_exempt": membership_exempt}
        eligible = all(flags.values())
        if not eligible:
            try:
                level = await self._member_level(settings, user, force=True)
                eligible = self._qualification(level, await self.policy(bot, scope), flags) is None
            except ScopeError:
                eligible = False
        status = grant["status"] if eligible else "SUSPENDED"
        async with self.db.transaction():
            await self._ensure_current(settings)
            changed = await self.db.execute(
                """UPDATE grants_v2 SET level_exempt = ?, membership_exempt = ?, status = ?,
                   updated_at = ?, revision = revision + 1
                   WHERE bot_self_id = ? AND user_id = ? AND scope = ? AND revision = ? AND status != 'REVOKED'""",
                (level_exempt, membership_exempt, status, int(time.time()), bot, user, scope, grant["revision"]),
            )
            if changed.rowcount != 1:
                raise ScopeError("授权已变更，请刷新。")
            await self._log(bot, actor, "OWNER", "LLM_EXEMPTIONS_UPDATED", user=user, scope=scope,
                            primary=settings.primary_group_id, status=status, **flags)
        return f"已更新{LABELS[scope]}豁免，授权状态 {status}。"

    async def restore(self, bot, user, scope, actor, role):
        settings = await self.settings(bot)
        grant = await self.grant(bot, user, scope)
        if not grant or grant["status"] != "SUSPENDED":
            raise ScopeError("只有暂停的授权可以恢复。已撤销的权限需 Owner 明确重新授予。")
        policy = await self.policy(bot, scope)
        level = None if grant["level_exempt"] and grant["membership_exempt"] else await self._member_level(
            settings, user, force=True
        )
        denied = self._qualification(level, policy, grant)
        if denied:
            raise ScopeError(denied)
        async with self.db.transaction():
            await self._ensure_current(settings)
            current = await self.grant(bot, user, scope)
            if (not current or current["revision"] != grant["revision"]
                    or await self._restricted(bot, user, scope)):
                raise ScopeError("授权已变更，请刷新。")
            await self._restore(current, settings, actor, role)
        return f"已恢复{LABELS[scope]}授权。"

    async def restrict(self, bot, user, scopes, actor, role, *, kind="REVOKED", remove=False):
        if kind not in {"REVOKED", "BLACKLIST"} or (remove and kind != "BLACKLIST"):
            raise ScopeError("人工撤销只能由 Owner 明确重新授权解除。")
        async with self.db.transaction():
            unbanned = False
            for scope in scopes:
                if remove:
                    changed = await self.db.execute(
                        "DELETE FROM grant_restrictions WHERE bot_self_id = ? AND user_id = ? AND scope = ? AND kind = ?",
                        (bot, user, scope, kind),
                    )
                    unbanned |= changed.rowcount > 0
                else:
                    changed = await self.db.execute(
                        "INSERT OR IGNORE INTO grant_restrictions VALUES (?, ?, ?, ?)", (bot, user, scope, kind)
                    )
                    await self.db.execute(
                        """UPDATE applications_v2 SET status = 'CANCELLED', reviewed_at = ?,
                           reviewer_id = ?, review_note = ?
                           WHERE bot_self_id = ? AND user_id = ? AND scope = ? AND status = 'PENDING'""",
                        (int(time.time()), actor, "黑名单限制" if kind == "BLACKLIST" else "管理员撤销授权",
                         bot, user, scope),
                    )
                if changed.rowcount > 0:
                    await self.db.execute(
                        """UPDATE grants_v2 SET status = CASE WHEN ? = 'REVOKED' THEN 'REVOKED' ELSE status END,
                           revision = revision + 1, updated_at = ?
                           WHERE bot_self_id = ? AND user_id = ? AND scope = ?""",
                        (kind, int(time.time()), bot, user, scope),
                    )
                    await self._log(bot, actor, role, "LLM_RESTRICTION_UPDATED", user=user, scope=scope,
                                    kind=kind, removed=remove)
            if unbanned:
                # Keep an invalidation marker so a lookup that started before
                # unban cannot refill the cache or be shared by a newer check.
                await self.db.execute(
                    """INSERT INTO primary_group_levels
                           (bot_self_id, primary_group_id, user_id, primary_revision, level, verified_at, lookup_epoch)
                       SELECT bot_self_id, primary_group_id, ?, primary_revision, 0, 0, 1 FROM bot_settings
                       WHERE bot_self_id = ? AND primary_group_id != ''
                       ON CONFLICT(bot_self_id, primary_group_id, user_id) DO UPDATE SET
                         verified_at = 0, lookup_epoch = primary_group_levels.lookup_epoch + 1""",
                    (user, bot),
                )
        return "已更新限制。"

    async def overview(self, bot: str, *, user: str = "", include_audit: bool = False) -> dict:
        settings = await self.settings(bot)
        scope = " AND user_id = ?" if user else ""
        params = (bot, user) if user else (bot,)
        applications = await self.db.fetchall(
            "SELECT * FROM applications_v2 WHERE bot_self_id = ?" + scope
            + " ORDER BY status != 'PENDING', created_at DESC LIMIT 100", params
        )
        grants = await self.db.fetchall(
            "SELECT * FROM grants_v2 WHERE bot_self_id = ?" + scope + " ORDER BY updated_at DESC LIMIT 100", params
        )
        levels = await self.db.fetchall(
            "SELECT user_id, level, verified_at FROM primary_group_levels WHERE bot_self_id = ?"
            " AND primary_group_id = ? AND primary_revision = ? AND verified_at > 0" + scope + " ORDER BY verified_at DESC LIMIT 100",
            (bot, settings.primary_group_id, settings.primary_revision, *((user,) if user else ())),
        )
        data = {
            "settings": asdict(settings),
            "policies": {s: asdict(await self.policy(bot, s)) for s in SCOPES},
            "applications": [dict(row) for row in applications],
            "grants": [dict(row) for row in grants],
            "levels": [dict(row) for row in levels],
        }
        if include_audit:
            entries = await self.audit.query(bot_self_id=bot, target_user_id=user or None, limit=100)
            data["audit"] = [entry.to_dict() for entry in entries]
        return data
