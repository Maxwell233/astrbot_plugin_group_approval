"""AstrBot Plugin: Group Approval (群等级准入与管理员审批).

Main entrypoint integrating Star lifecycle, message gate, commands, and WebUI API.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from .approval import (
    AccessService,
    AdmissionGate,
    ApprovalService,
    AuditService,
    CommandDispatcher,
    Database,
    LevelService,
    NapCatClient,
    NotificationService,
    PluginConfig,
    WebApiHandler,
)

logger = logging.getLogger("astrbot_plugin_group_approval")

PLUGIN_NAME = "astrbot_plugin_group_approval"

# Graceful AstrBot import with standalone fallback
try:
    from astrbot.api.event import AstrMessageEvent, filter
    from astrbot.api.star import Context, Star, StarTools
    from astrbot.api.web import json_response, request
    ASTRBOT_AVAILABLE = True
except ImportError:
    ASTRBOT_AVAILABLE = False

    class Star:  # type: ignore
        def __init__(self, context: Any = None):
            self.context = context

    class Context:  # type: ignore
        pass

    class AstrTools:  # type: ignore
        @staticmethod
        def get_data_dir() -> Path:
            return Path("data")

    StarTools = AstrTools  # type: ignore

    class _FilterMock:
        class EventMessageType:
            ALL = "ALL"

        def event_message_type(self, *args: Any, **kwargs: Any) -> Any:
            def dec(f: Any) -> Any:
                return f
            return dec

        def command(self, *args: Any, **kwargs: Any) -> Any:
            def dec(f: Any) -> Any:
                return f
            return dec

    filter = _FilterMock()  # type: ignore
    AstrMessageEvent = Any  # type: ignore

    def json_response(data: Any, *args: Any, **kwargs: Any) -> Any:
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

        # 2. Resolve persistent data directory
        if ASTRBOT_AVAILABLE and hasattr(StarTools, "get_data_dir"):
            try:
                self.data_dir = StarTools.get_data_dir()
            except Exception:
                self.data_dir = Path("data") / PLUGIN_NAME
        else:
            self.data_dir = Path("data") / PLUGIN_NAME

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "group_approval.db"

        # 3. Instantiate database & core services
        self.db = Database(self.db_path)
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
        self.approval_service = ApprovalService(
            db=self.db,
            level_service=self.level_service,
            audit_service=self.audit_service,
            notification_service=self.notification_service,
            config=self.plugin_config,
        )
        self.access_service = AccessService(
            db=self.db,
            approval_service=self.approval_service,
            level_service=self.level_service,
            audit_service=self.audit_service,
            config=self.plugin_config,
        )
        self.dispatcher = CommandDispatcher(
            db=self.db,
            approval_service=self.approval_service,
            audit_service=self.audit_service,
            config=self.plugin_config,
        )
        self.gate = AdmissionGate(self.access_service, self.plugin_config)
        self.web_api = WebApiHandler(
            db=self.db,
            approval_service=self.approval_service,
            audit_service=self.audit_service,
            config=self.plugin_config,
        )

        # 4. Schedule startup initialization task
        asyncio.create_task(self._init_plugin())

        # 5. Register Web API routes with AstrBot WebUI
        self._register_web_routes()

    async def _init_plugin(self) -> None:
        """Asynchronously initialize SQLite schema and background workers."""
        try:
            await self.db.init()
            # Clean expired records on boot
            await self.approval_service.clean_expired()
            # Start persistent outbox notification retry worker
            if self.plugin_config.notifications.retry_enabled:
                self.notification_service.start_worker()
            logger.info("GroupApprovalPlugin initialized successfully.")
        except Exception as e:
            logger.error(f"Failed to initialize GroupApprovalPlugin: {e}", exc_info=True)

    async def terminate(self) -> None:
        """Clean up background workers and close database connections upon unloading."""
        logger.info("Terminating GroupApprovalPlugin...")
        try:
            await self.notification_service.stop_worker()
            await self.db.close()
            logger.info("GroupApprovalPlugin terminated cleanly.")
        except Exception as e:
            logger.error(f"Error during GroupApprovalPlugin termination: {e}")

    def _register_web_routes(self) -> None:
        """Register routes with AstrBot Quart Web framework."""
        if self.context is None or not hasattr(self.context, "register_web_api"):
            return

        def wrap(fn: Any) -> Any:
            async def handler(*args: Any, **kwargs: Any) -> Any:
                res = await fn(*args, **kwargs)
                return json_response(res)
            return handler

        routes = [
            (f"/{PLUGIN_NAME}/dashboard", self._web_dashboard, ["GET"], "Dashboard metrics"),
            (f"/{PLUGIN_NAME}/applications", self._web_applications, ["GET"], "List applications"),
            (f"/{PLUGIN_NAME}/applications/approve", self._web_approve, ["POST"], "Approve application"),
            (f"/{PLUGIN_NAME}/applications/reject", self._web_reject, ["POST"], "Reject application"),
            (f"/{PLUGIN_NAME}/users", self._web_users, ["GET"], "List user grants"),
            (f"/{PLUGIN_NAME}/users/grant", self._web_manual_grant, ["POST"], "Manual grant"),
            (f"/{PLUGIN_NAME}/users/revoke", self._web_revoke_grant, ["POST"], "Revoke grant"),
            (f"/{PLUGIN_NAME}/users/ban", self._web_ban, ["POST"], "Ban user"),
            (f"/{PLUGIN_NAME}/users/unban", self._web_unban, ["POST"], "Unban user"),
            (f"/{PLUGIN_NAME}/groups", self._web_groups, ["GET"], "List group policies"),
            (f"/{PLUGIN_NAME}/groups/update", self._web_update_group, ["POST"], "Update group policy"),
            (f"/{PLUGIN_NAME}/audit_logs", self._web_audit_logs, ["GET"], "List audit logs"),
        ]

        for path, handler_fn, methods, desc in routes:
            try:
                self.context.register_web_api(path, wrap(handler_fn), methods, desc)
            except Exception as e:
                logger.warning(f"Could not register web route {path}: {e}")

    # --- Web Route Handlers ---
    async def _web_dashboard(self) -> dict[str, Any]:
        return await self.web_api.get_dashboard_stats()

    async def _web_applications(self) -> dict[str, Any]:
        gid = getattr(request, "args", {}).get("group_id")
        status = getattr(request, "args", {}).get("status")
        uid = getattr(request, "args", {}).get("user_id")
        return await self.web_api.list_applications(group_id=gid, status=status, user_id=uid)

    async def _web_approve(self) -> dict[str, Any]:
        data = await request.get_json() if hasattr(request, "get_json") else {}
        return await self.web_api.approve_application(data or {})

    async def _web_reject(self) -> dict[str, Any]:
        data = await request.get_json() if hasattr(request, "get_json") else {}
        return await self.web_api.reject_application(data or {})

    async def _web_users(self) -> dict[str, Any]:
        gid = getattr(request, "args", {}).get("group_id")
        uid = getattr(request, "args", {}).get("user_id")
        status = getattr(request, "args", {}).get("status")
        return await self.web_api.list_users(group_id=gid, user_id=uid, status=status)

    async def _web_manual_grant(self) -> dict[str, Any]:
        data = await request.get_json() if hasattr(request, "get_json") else {}
        return await self.web_api.manual_grant(data or {})

    async def _web_revoke_grant(self) -> dict[str, Any]:
        data = await request.get_json() if hasattr(request, "get_json") else {}
        return await self.web_api.revoke_grant(data or {})

    async def _web_ban(self) -> dict[str, Any]:
        data = await request.get_json() if hasattr(request, "get_json") else {}
        return await self.web_api.ban_user(data or {})

    async def _web_unban(self) -> dict[str, Any]:
        data = await request.get_json() if hasattr(request, "get_json") else {}
        return await self.web_api.unban_user(data or {})

    async def _web_groups(self) -> dict[str, Any]:
        return await self.web_api.list_groups()

    async def _web_update_group(self) -> dict[str, Any]:
        data = await request.get_json() if hasattr(request, "get_json") else {}
        return await self.web_api.update_group_policy(data or {})

    async def _web_audit_logs(self) -> dict[str, Any]:
        gid = getattr(request, "args", {}).get("group_id")
        uid = getattr(request, "args", {}).get("user_id")
        action = getattr(request, "args", {}).get("action")
        return await self.web_api.list_audit_logs(group_id=gid, user_id=uid, action=action)

    # --- Priority 50 Admission Gate ---
    @filter.event_message_type(filter.EventMessageType.ALL, priority=50)
    async def on_message_gate(self, event: AstrMessageEvent) -> None:
        """Intercept ordinary business and AI requests from unauthorized users.

        Runs at priority=50, after GroupGuardian (100) and before normal plugins (0).
        """
        bot_self_id = str(event.get_self_id() or getattr(getattr(event, "message_obj", None), "self_id", "default")).strip()
        group_id = event.get_group_id()
        user_id = str(event.get_sender_id() or "").strip()
        message_str = getattr(event, "message_str", "") or ""

        # Check @bot invocation
        is_at_bot = False
        msg_obj = getattr(event, "message_obj", None)
        if msg_obj and hasattr(msg_obj, "message"):
            for comp in getattr(msg_obj, "message", []):
                if getattr(comp, "type", "") == "at" and str(getattr(comp, "qq", "")) == bot_self_id:
                    is_at_bot = True
                    break

        # Check if sender is QQ group owner or admin
        is_group_admin = False
        sender_obj = getattr(msg_obj, "sender", None)
        if sender_obj and getattr(sender_obj, "role", "member") in ("owner", "admin"):
            is_group_admin = True

        intercept, notice = await self.gate.should_intercept(
            bot_self_id=bot_self_id,
            group_id=group_id,
            user_id=user_id,
            message_str=message_str,
            is_at_bot=is_at_bot,
            is_group_admin=is_group_admin,
        )

        if intercept:
            # Terminate event pipeline to stop normal plugins and LLM
            event.stop_event()
            if notice:
                yield event.plain_result(notice)

    # --- User Commands ---
    @filter.command("申请使用")
    async def cmd_apply(self, event: AstrMessageEvent) -> None:
        bot_self_id = str(event.get_self_id() or getattr(getattr(event, "message_obj", None), "self_id", "default")).strip()
        group_id = event.get_group_id() or ""
        user_id = str(event.get_sender_id() or "").strip()
        reason = getattr(event, "message_str", "").replace("/申请使用", "").replace("申请使用", "").strip()

        reply = await self.dispatcher.handle_apply(bot_self_id, group_id, user_id, reason)
        yield event.plain_result(reply)

    @filter.command("我的权限")
    async def cmd_my_status(self, event: AstrMessageEvent) -> None:
        bot_self_id = str(event.get_self_id() or getattr(getattr(event, "message_obj", None), "self_id", "default")).strip()
        group_id = event.get_group_id() or ""
        user_id = str(event.get_sender_id() or "").strip()

        reply = await self.dispatcher.handle_my_status(bot_self_id, group_id, user_id)
        yield event.plain_result(reply)

    @filter.command("申请状态")
    async def cmd_application_status(self, event: AstrMessageEvent) -> None:
        bot_self_id = str(event.get_self_id() or getattr(getattr(event, "message_obj", None), "self_id", "default")).strip()
        group_id = event.get_group_id() or ""
        user_id = str(event.get_sender_id() or "").strip()

        reply = await self.dispatcher.handle_application_status(bot_self_id, group_id, user_id)
        yield event.plain_result(reply)

    @filter.command("取消申请")
    async def cmd_cancel_application(self, event: AstrMessageEvent) -> None:
        bot_self_id = str(event.get_self_id() or getattr(getattr(event, "message_obj", None), "self_id", "default")).strip()
        group_id = event.get_group_id() or ""
        user_id = str(event.get_sender_id() or "").strip()

        reply = await self.dispatcher.handle_cancel_application(bot_self_id, group_id, user_id)
        yield event.plain_result(reply)

    @filter.command("使用规则")
    async def cmd_rules(self, event: AstrMessageEvent) -> None:
        bot_self_id = str(event.get_self_id() or getattr(getattr(event, "message_obj", None), "self_id", "default")).strip()
        group_id = event.get_group_id() or ""

        reply = await self.dispatcher.handle_rules(bot_self_id, group_id)
        yield event.plain_result(reply)

    # --- Administrator Commands (/ga ...) ---
    @filter.command("ga")
    async def cmd_ga(self, event: AstrMessageEvent) -> None:
        bot_self_id = str(event.get_self_id() or getattr(getattr(event, "message_obj", None), "self_id", "default")).strip()
        sender_id = str(event.get_sender_id() or "").strip()
        group_id = event.get_group_id()

        msg = getattr(event, "message_str", "").strip()
        parts = [p for p in msg.split() if p]
        args = parts[1:] if len(parts) > 1 else []

        reply = await self.dispatcher.handle_admin_command(
            bot_self_id=bot_self_id,
            sender_id=sender_id,
            group_id=group_id,
            args=args,
        )
        yield event.plain_result(reply)
