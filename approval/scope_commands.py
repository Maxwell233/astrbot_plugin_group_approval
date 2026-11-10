"""QQ command parsing and shared administrative validation for the scoped API."""

from __future__ import annotations

from typing import Any

from .scope_service import LABELS, SCOPES, ScopeError, ScopeService, parse_scope, valid_qq

_OWNER_ACTIONS = {"configure", "grant", "exemptions", "ban", "unban", "reviewer_add", "reviewer_remove"}
_OPTION_FLAGS = ("group_to_private", "private_to_group", "owner_bypass")
_POLICY_FIELDS = ("min_level", "approval_required")


def _bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"on", "off"}:
        return value.lower() == "on"
    raise ScopeError(f"{field} 需要 on/off。")


def _int(value: Any, field: str, maximum: int) -> int:
    raw = str(value).strip()
    if not raw.isascii() or not raw.isdecimal() or len(raw) > 4 or int(raw) > maximum:
        raise ScopeError(f"{field} 必须在 0 到 {maximum} 之间。")
    return int(raw)


def _qq(value: Any, field: str) -> str:
    text = str(value).strip()
    if not valid_qq(text):
        raise ScopeError(f"{field} 格式无效。")
    return text


def _note(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 500:
        raise ScopeError("备注最多 500 字。")
    return value.strip()


class ScopeCommands:
    def __init__(self, service: ScopeService):
        self.service = service

    async def action(self, bot: str, actor: str, role: str, action: str, payload: dict) -> str:
        """Both transports must authenticate and verify Bot identity first."""
        if role not in {"OWNER", "REVIEWER"} or (action in _OWNER_ACTIONS and role != "OWNER"):
            raise ScopeError("需要相应的 Owner 或 Reviewer 权限。")
        if action == "configure":
            options = {key: _bool(payload[key], key) for key in _OPTION_FLAGS if key in payload}
            if "cache_ttl" in payload:
                options["cache_ttl"] = _int(payload["cache_ttl"], "缓存秒数", 3600)
            primary = _qq(payload["primary_group_id"], "主群号") if "primary_group_id" in payload else None
            policies = {}
            for scope in SCOPES:
                raw = payload.get(scope.lower(), {})
                if not isinstance(raw, dict) or set(raw) - set(_POLICY_FIELDS):
                    raise ScopeError("域策略格式无效。")
                if raw:
                    policies[scope] = {}
                if "min_level" in raw:
                    policies[scope]["min_level"] = _int(raw["min_level"], "最低等级", 1000)
                if "approval_required" in raw:
                    policies[scope]["approval_required"] = _bool(raw["approval_required"], "审批")
            await self.service.configure(bot, actor, primary_group_id=primary, policies=policies, **options)
            return "已保存 Bot 的 AI 准入设置。"
        if action in {"approve", "reject"}:
            app_id = str(payload.get("application_id", "")).strip()
            if len(app_id) != 13 or not app_id.startswith("L"):
                raise ScopeError("申请编号无效。")
            return await self.service.review(
                bot, app_id, actor, role, approve=action == "approve", note=_note(payload.get("note", ""))
            )
        user = _qq(payload.get("user_id", ""), "QQ")
        if action in {"reviewer_add", "reviewer_remove"}:
            return await self.service.reviewers.set_reviewer(user, actor, enabled=action == "reviewer_add")
        if action in {"ban", "unban", "revoke"}:
            raw_scope = str(payload.get("scope", "")).lower()
            scopes = SCOPES if raw_scope == "all" else (parse_scope(raw_scope),)
            return await self.service.restrict(
                bot, user, scopes, actor, role,
                kind="REVOKED" if action == "revoke" else "BLACKLIST", remove=action == "unban",
            )
        scope = parse_scope(payload.get("scope", ""))
        if action == "restore":
            return await self.service.restore(bot, user, scope, actor, role)
        if action in {"grant", "exemptions"}:
            method = self.service.manual_grant if action == "grant" else self.service.set_exemptions
            return await method(
                bot, user, scope, actor,
                level_exempt=_bool(payload.get("level_exempt", False), "等级豁免"),
                membership_exempt=_bool(payload.get("membership_exempt", False), "成员豁免"),
            )
        raise ScopeError("未知管理操作。")

    async def user(self, bot: str, user: str, is_private: bool, command: str, args: list[str],
                   group: str) -> str:
        scope = "PRIVATE" if is_private else "GROUP"
        if command in {"申请群聊", "申请私聊", "申请使用"}:
            if command != "申请使用":
                scope = "GROUP" if command == "申请群聊" else "PRIVATE"
            return await self.service.apply(bot, user, scope, group, _note(" ".join(args)))
        if command == "取消申请":
            return await self.service.cancel(bot, user, parse_scope(args[0]) if args else scope)
        data = await self.service.overview(bot, user=user)
        if command == "使用规则":
            return "\n".join(
                f"{LABELS[s]} AI：主群等级 LV{p['min_level']}，{'需要审批' if p['approval_required'] else '主动申请后核验'}"
                for s, p in data["policies"].items()
            )
        if command == "申请状态":
            if not is_private:
                return "请私聊 Bot 查询你的申请状态。"
            rows = [f"{a['id']} {LABELS[a['scope']]} {a['status']}" for a in data["applications"][:8]]
            return "\n".join(rows) or "暂无申请记录。"
        states = {g["scope"]: g["status"] for g in data["grants"]}
        return "\n".join(f"{LABELS[s]} AI：{states.get(s, '未授权')}" for s in SCOPES)

    async def admin(self, bot: str, user: str, private: bool, args: list[str]) -> str:
        if not self.service.config.private_chat.admin_commands_enabled:
            return "此 Bot 的管理指令已关闭。"
        if not private:
            return "请私聊 Bot 使用管理指令。"
        if not await self.service.reviewers.can_review(user):
            return "需要 Owner 或 Reviewer 权限。"
        role = "OWNER" if self.service.config.is_owner(user) else "REVIEWER"
        if not args:
            return (
                "管理命令：pending / approve / reject / grant / revoke / restore / "
                "exemptions / level / approval / primary-group / cross-grant / user / status / ban / unban / reviewer"
            )
        name, *tail = args
        name = name.lower()
        match name, tail:
            case "pending", [] | [_]:
                selected = parse_scope(tail[0]) if tail else None
                data = await self.service.overview(bot)
                rows = [f"{a['id']} {a['scope']} QQ {a['user_id']} LV{a['level']} {a['reason']}" for a in data["applications"]
                        if a["status"] == "PENDING" and (not selected or a["scope"] == selected)]
                return "\n".join(rows[:20]) or "暂无待审申请。"
            case "approve" | "reject", [app_id, *note]:
                return await self.action(bot, user, role, name, {"application_id": app_id, "note": " ".join(note)})
            case "grant", [target, scope, *flags] if set(flags) <= {"--level-exempt", "--membership-exempt"}:
                return await self.action(bot, user, role, name, {
                    "user_id": target, "scope": scope, "level_exempt": "--level-exempt" in flags,
                    "membership_exempt": "--membership-exempt" in flags,
                })
            case "exemptions", [target, scope, level, membership]:
                return await self.action(bot, user, role, name, {
                    "user_id": target, "scope": scope, "level_exempt": level, "membership_exempt": membership,
                })
            case "revoke" | "restore" | "ban" | "unban", [target, scope]:
                return await self.action(bot, user, role, name, {"user_id": target, "scope": scope})
            case "level" | "approval", [scope, value]:
                field = "min_level" if name == "level" else "approval_required"
                return await self.action(bot, user, role, "configure", {parse_scope(scope).lower(): {field: value}})
            case "primary-group", ["set", group]:
                return await self.action(bot, user, role, "configure", {"primary_group_id": group})
            case "primary-group", ["show"]:
                return f"主群：{(await self.service.settings(bot)).primary_group_id or '未设置'}"
            case "cross-grant", [direction, value] if direction in {"group-to-private", "private-to-group"}:
                return await self.action(bot, user, role, "configure", {direction.replace("-", "_"): value})
            case "reviewer", ["add" | "remove" as operation, target]:
                return await self.action(bot, user, role, f"reviewer_{operation}", {"user_id": target})
            case "user", [target]:
                data = await self.service.overview(bot, user=_qq(target, "QQ"))
                rows = [f"{g['scope']} {g['status']}（{g['source']}）" for g in data["grants"]]
                if data["levels"]:
                    level = data["levels"][0]
                    rows.append(f"最近主群等级 LV{level['level']}，核验时间 {level['verified_at']}")
                return "\n".join(rows) or "暂无授权或等级记录。"
            case "status", []:
                data = await self.service.overview(bot)
                pending = sum(row["status"] == "PENDING" for row in data["applications"])
                return f"主群 {data['settings']['primary_group_id'] or '未设置'}，最近记录中 {pending} 条待审批。"
        raise ScopeError("指令参数无效，发送 /ga 查看可用命令。")
