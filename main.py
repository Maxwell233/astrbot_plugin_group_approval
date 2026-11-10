"""AstrBot plugin entry point for Bot-wide native LLM chat admission."""

from __future__ import annotations

import asyncio
from enum import Enum
from pathlib import Path
from typing import Any

from astrbot.api import logger

try:
    from .approval import (
        AuditService,
        AuthorizationService,
        Database,
        LevelService,
        NapCatClient,
        NotificationService,
        PluginConfig,
        ReviewerService,
        __version__,
    )
    from .approval.auth_service import build_dashboard_username_getter
    from .approval.native_llm import NativeLLMGuard
    from .approval.scope_commands import ScopeCommands
    from .approval.scope_service import ScopeError, ScopeService, valid_qq
except ImportError:
    from approval import (
        AuditService,
        AuthorizationService,
        Database,
        LevelService,
        NapCatClient,
        NotificationService,
        PluginConfig,
        ReviewerService,
        __version__,
    )
    from approval.auth_service import build_dashboard_username_getter
    from approval.native_llm import NativeLLMGuard
    from approval.scope_commands import ScopeCommands
    from approval.scope_service import ScopeError, ScopeService, valid_qq

PLUGIN_NAME = "astrbot_plugin_group_approval"

# Graceful AstrBot import with standalone fallback
try:
    from astrbot.api.event import AstrMessageEvent, filter
    from astrbot.api.star import Star, StarTools
    from astrbot.api.web import json_response, request
    from astrbot.core.platform.message_type import MessageType

    ASTRBOT_AVAILABLE = True
except ImportError:
    ASTRBOT_AVAILABLE = False

    class Star:  # type: ignore
        def __init__(self, context: Any = None):
            self.context = context

    StarTools = None  # type: ignore

    class MessageType(Enum):  # type: ignore
        GROUP_MESSAGE = "GroupMessage"
        FRIEND_MESSAGE = "FriendMessage"
        OTHER_MESSAGE = "OtherMessage"

    class _FilterMock:
        class PlatformAdapterType:
            AIOCQHTTP = "aiocqhttp"

        def command(self, *args: Any, **kwargs: Any) -> Any:
            def dec(f: Any) -> Any:
                return f

            return dec

        platform_adapter_type = command

    filter = _FilterMock()  # type: ignore
    AstrMessageEvent = Any  # type: ignore

    def json_response(data: Any, *args: Any, status: int = 200, **kwargs: Any) -> Any:
        if isinstance(data, dict):
            data["_status_code"] = status
        return data

    request = Any  # type: ignore

class GroupApprovalPlugin(Star):
    """Group Approval Star Plugin."""

    def __init__(self, context: Any = None, config: dict[str, Any] | None = None):
        super().__init__(context)
        self.context = context

        # 1. Parse configuration
        raw_cfg = getattr(config, "to_dict", lambda: config)() if config else {}
        self.plugin_config = PluginConfig.from_dict(raw_cfg)

        data_dir = StarTools.get_data_dir(PLUGIN_NAME) if ASTRBOT_AVAILABLE else Path("data") / PLUGIN_NAME
        self.db = Database(data_dir / "group_approval.db")
        self.napcat = NapCatClient()
        if context is not None:
            self.napcat.set_context(context)

        self.level_service = LevelService(self.napcat)
        self.audit_service = AuditService(self.db)
        self.notification_service = NotificationService(
            db=self.db,
            client=self.napcat,
            max_retries=self.plugin_config.notifications.max_retries,
            retry_interval_seconds=self.plugin_config.notifications.retry_interval_seconds,
        )
        self.reviewers = ReviewerService(self.db, self.plugin_config)
        self.scopes = ScopeService(
            self.db, self.level_service, self.audit_service, self.notification_service,
            self.plugin_config, self.reviewers,
        )
        self.commands = ScopeCommands(self.scopes)
        self.native_guard = NativeLLMGuard(self._check_native_chat)
        self.auth = AuthorizationService(
            dashboard_username_getter=build_dashboard_username_getter(context),
        )
        self._initialized = False
        self._web_handlers = []
        self._web_requests: set[asyncio.Future[None]] = set()

    async def initialize(self) -> None:
        if self._initialized:
            return
        await self.db.init()
        try:
            keep_days = self.plugin_config.security.keep_audit_days
            if keep_days > 0:
                await self.audit_service.cleanup_old_logs(keep_days)
            if ASTRBOT_AVAILABLE and self.plugin_config.enabled:
                self.native_guard.install()
            self._register_web_routes()
            if self.plugin_config.notifications.retry_enabled:
                self.notification_service.start_worker()
            self._initialized = True
        except BaseException:
            self._unregister_web_routes()
            await self.native_guard.uninstall()
            await self.db.close()
            raise
        logger.info("Group Approval %s initialized. Native LLM guard active: %s", __version__, self.native_guard.installed)

    async def terminate(self) -> None:
        self._initialized = False
        self._unregister_web_routes()
        await self.native_guard.uninstall()
        if self._web_requests:
            await asyncio.wait(self._web_requests)
        try:
            await self.notification_service.stop_worker()
        finally:
            await self.db.close()

    async def _event_identity(self, event: AstrMessageEvent) -> tuple[str, str]:
        bot = str(event.get_self_id() or "").strip()
        user = str(event.get_sender_id() or "").strip()
        if not valid_qq(bot) or not valid_qq(user) or not await self.napcat.has_verified_bot(
            bot, event.get_platform_id()
        ):
            raise ScopeError("无法核验当前 OneBot 或 QQ 用户身份。")
        return bot, user

    async def _check_native_chat(self, event: AstrMessageEvent) -> str | None:
        if event.get_message_type() not in (MessageType.GROUP_MESSAGE, MessageType.FRIEND_MESSAGE):
            return "无法确认原生 AI 聊天的会话类型。"
        try:
            bot, user = await self._event_identity(event)
        except ScopeError as exc:
            return str(exc)
        if event.is_private_chat():
            return await self.scopes.check_chat(bot, user, "PRIVATE")
        if not event.get_group_id():
            return "无法确认当前 QQ 群会话。"
        return await self.scopes.check_chat(bot, user, "GROUP")

    def _register_web_routes(self) -> None:
        if self.context is None:
            return

        def wrap(fn):
            async def handler():
                if not self._initialized:
                    return json_response(self._err("插件当前不可用。", 503))
                done = asyncio.get_running_loop().create_future()
                self._web_requests.add(done)
                try:
                    return json_response(await fn())
                finally:
                    self._web_requests.remove(done)
                    done.set_result(None)

            self._web_handlers.append(handler)
            return handler

        for path, method, fn in (
            ("bots", "GET", self._web_bots),
            ("scope/state", "GET", self._web_state),
            ("scope/action", "POST", self._web_action),
        ):
            self.context.register_web_api(f"/{PLUGIN_NAME}/{path}", wrap(fn), [method], path)

    def _unregister_web_routes(self) -> None:
        if self._web_handlers:
            # AstrBot 4.29.4 has no unregister API. Preserve any newer registrations.
            routes = self.context.registered_web_apis
            routes[:] = [entry for entry in routes if all(entry[1] is not handler for handler in self._web_handlers)]
            self._web_handlers.clear()

    async def _dashboard_owner(self) -> str | None:
        operator = self.auth.get_authenticated_user(request)
        if operator and await self.auth.get_user_role(operator) == "OWNER":
            return operator
        return None

    @staticmethod
    def _ok(data):
        return {"status": "ok", "retcode": 0, "data": data}

    @staticmethod
    def _err(message, code=400):
        return {"status": "error", "retcode": code, "message": message}

    async def _web_bots(self):
        if not await self._dashboard_owner():
            return self._err("需要 AstrBot Dashboard 管理员登录。", 403)
        status = self.native_guard.status
        return self._ok({
            "verified_bots": await self.napcat.list_bots(),
            "guard_active": status == "ACTIVE",
            "guard_status": status,
        })

    async def _web_state(self):
        if not await self._dashboard_owner():
            return self._err("需要 AstrBot Dashboard 管理员登录。", 403)
        bot = str(request.query.get("bot_self_id", "")).strip()
        if not valid_qq(bot) or bot not in await self.napcat.list_bots():
            return self._err("请选择已核验的 OneBot 账号。", 403)
        user = str(request.query.get("user_id", "")).strip()
        if user and not valid_qq(user):
            return self._err("QQ 格式无效。")
        return self._ok(await self.scopes.overview(bot, user=user, include_audit=True))

    async def _web_action(self):
        operator = await self._dashboard_owner()
        if not operator:
            return self._err("需要 AstrBot Dashboard 管理员登录。", 403)
        payload = await request.json()
        if not isinstance(payload, dict):
            return self._err("请求正文必须为 JSON 对象。")
        bot = str(payload.get("bot_self_id", "")).strip()
        if not valid_qq(bot) or not await self.napcat.verify_bot(bot):
            return self._err("当前 OneBot 账号未通过连接核验。", 403)
        try:
            result = await self.commands.action(bot, operator, "OWNER", str(payload.get("action", "")), payload)
        except ScopeError as exc:
            return self._err(str(exc))
        return self._ok({"message": result})

    async def _command(self, event: AstrMessageEvent, name: str) -> str | None:
        if not self.plugin_config.enabled or event.get_platform_name() != "aiocqhttp":
            return None
        # Suppress only this command's implicit native chat; the next plugin
        # handler can still run, including one that explicitly yields a request.
        event.should_call_llm(True)
        try:
            bot, user = await self._event_identity(event)
            args = (event.message_str or "").split()[1:]
            if name == "ga":
                return await self.commands.admin(bot, user, event.is_private_chat(), args)
            return await self.commands.user(bot, user, event.is_private_chat(), name, args, event.get_group_id() or "")
        except ScopeError as exc:
            return str(exc)

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.command("申请群聊")
    async def cmd_apply_group(self, event: AstrMessageEvent):
        reply = await self._command(event, "申请群聊")
        if reply is not None:
            yield event.plain_result(reply)

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.command("申请私聊")
    async def cmd_apply_private(self, event: AstrMessageEvent):
        reply = await self._command(event, "申请私聊")
        if reply is not None:
            yield event.plain_result(reply)

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.command("申请使用")
    async def cmd_apply(self, event: AstrMessageEvent):
        reply = await self._command(event, "申请使用")
        if reply is not None:
            yield event.plain_result(reply)

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.command("我的权限")
    async def cmd_my_status(self, event: AstrMessageEvent):
        reply = await self._command(event, "我的权限")
        if reply is not None:
            yield event.plain_result(reply)

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.command("申请状态")
    async def cmd_application_status(self, event: AstrMessageEvent):
        reply = await self._command(event, "申请状态")
        if reply is not None:
            yield event.plain_result(reply)

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.command("取消申请")
    async def cmd_cancel(self, event: AstrMessageEvent):
        reply = await self._command(event, "取消申请")
        if reply is not None:
            yield event.plain_result(reply)

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.command("使用规则")
    async def cmd_rules(self, event: AstrMessageEvent):
        reply = await self._command(event, "使用规则")
        if reply is not None:
            yield event.plain_result(reply)

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.command("ga")
    async def cmd_ga(self, event: AstrMessageEvent):
        reply = await self._command(event, "ga")
        if reply is not None:
            yield event.plain_result(reply)
