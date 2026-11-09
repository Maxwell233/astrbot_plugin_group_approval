"""AstrBot Plugin: Group Approval (群等级准入与管理员审批).

Main entrypoint integrating Star lifecycle, message gate, commands, and WebUI API.

Compatibility floor: **AstrBot >= 4.26.0**.
  * ``astrbot.api.web`` (``request``/``json_response``) does not exist before 4.26.0.
  * ``event.stop_event()`` does not stop later plugin handlers before 4.25.0.
The previous ``>=4.16.0`` claim was wrong; see COMPATIBILITY.md.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

try:
    from .approval import (
        AccessService,
        AdmissionGate,
        ApprovalService,
        AuditService,
        AuthorizationService,
        CommandDispatcher,
        Database,
        HandlerDiagnostics,
        LevelService,
        NapCatClient,
        NotificationService,
        PluginConfig,
        WebApiHandler,
    )
    from .approval.auth_service import build_dashboard_username_getter
    from .approval.gate import GATE_PRIORITY, AdmissionKind
    from .approval.web_api import parse_bool
except ImportError:
    from approval import (  # type: ignore
        AccessService,
        AdmissionGate,
        ApprovalService,
        AuditService,
        AuthorizationService,
        CommandDispatcher,
        Database,
        HandlerDiagnostics,
        LevelService,
        NapCatClient,
        NotificationService,
        PluginConfig,
        WebApiHandler,
    )
    from approval.auth_service import build_dashboard_username_getter  # type: ignore
    from approval.gate import GATE_PRIORITY, AdmissionKind  # type: ignore
    from approval.web_api import parse_bool  # type: ignore

logger = logging.getLogger("astrbot_plugin_group_approval")

PLUGIN_NAME = "astrbot_plugin_group_approval"
LIVINGMEMORY_CAPTURE_KEY = "group_approval.livingmemory_capture"

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
        class PlatformAdapterType:
            AIOCQHTTP = "aiocqhttp"

        class EventMessageType:
            ALL = "ALL"

        def event_message_type(self, *args: Any, **kwargs: Any) -> Any:
            def dec(f: Any) -> Any:
                return f

            return dec

        platform_adapter_type = event_message_type
        on_astrbot_loaded = event_message_type

        def command(self, *args: Any, **kwargs: Any) -> Any:
            def dec(f: Any) -> Any:
                return f

            return dec

    filter = _FilterMock()  # type: ignore
    AstrMessageEvent = Any  # type: ignore

    def json_response(data: Any, *args: Any, status: int = 200, **kwargs: Any) -> Any:
        if isinstance(data, dict):
            data["_status_code"] = status
        return data

    request = Any  # type: ignore

# Real AstrBot message types (only used to skip non-chat events such as notices/requests).
try:
    from astrbot.core.platform.message_type import MessageType as _RealMessageType
except ImportError:  # pragma: no cover - standalone test mode
    _RealMessageType = None


def _is_onebot_event(event: Any) -> bool:
    get_platform_name = getattr(event, "get_platform_name", None)
    return callable(get_platform_name) and get_platform_name() == "aiocqhttp"


def _bot_self_id(event: Any) -> str:
    """Return the verified bot QQ id from an inbound event, or '' when unavailable.

    NEVER substitutes a placeholder: a missing identity must fail closed, not silently bind
    business data to a fake bot id.
    """
    try:
        return str(event.get_self_id() or "").strip()
    except Exception:  # noqa: BLE001 - defensive for odd adapters
        return ""


def _group_id(event: Any) -> str | None:
    try:
        gid = event.get_group_id()
    except Exception:  # noqa: BLE001
        return None
    gid = str(gid).strip() if gid is not None else ""
    return gid or None


def _is_chat_message(event: Any, msg_obj: Any) -> bool:
    """Return True only for real chat messages (group/private), never notices/requests.

    AstrBot's ``EventMessageType.ALL`` also matches ``OTHER_MESSAGE``, so the gate must
    filter explicitly. On real AstrBot the authoritative source is
    ``event.get_message_type()``; the OneBot ``post_type`` fallback keeps the behaviour
    testable without the framework.
    """
    getter = getattr(event, "get_message_type", None)
    if callable(getter):
        try:
            message_type = getter()
        except Exception:  # noqa: BLE001
            message_type = None
        if message_type is not None:
            if _RealMessageType is not None:
                return message_type in (_RealMessageType.GROUP_MESSAGE, _RealMessageType.FRIEND_MESSAGE)
            name = str(message_type).upper()
            return name.endswith("GROUP_MESSAGE") or name.endswith("FRIEND_MESSAGE")

    for source in (msg_obj, getattr(event, "raw_message", None), getattr(event, "raw_event", None)):
        if source is None:
            continue
        post_type = source.get("post_type") if isinstance(source, dict) else getattr(source, "post_type", None)
        if post_type:
            return str(post_type).strip().lower() == "message"
    return True


def _get_query_param(req: Any, name: str, default: Any = None) -> Any:
    """Fetch query parameter supporting modern request.query and legacy request.args."""
    query = getattr(req, "query", None)
    if query and hasattr(query, "get"):
        val = query.get(name)
        if val is not None:
            return val
    args = getattr(req, "args", None)
    if args and hasattr(args, "get"):
        return args.get(name, default)
    return default


async def _get_request_json(req: Any) -> dict[str, Any]:
    """Parse request JSON payload supporting await request.json(default={}) and get_json()."""
    if hasattr(req, "json"):
        try:
            res = req.json(default={}) if callable(req.json) else req.json
            if asyncio.iscoroutine(res):
                res = await res
            if isinstance(res, dict):
                return res
        except Exception:  # noqa: BLE001 - malformed body => empty payload
            pass
    if hasattr(req, "get_json"):
        try:
            res = req.get_json()
            if asyncio.iscoroutine(res):
                res = await res
            if isinstance(res, dict):
                return res
        except Exception:  # noqa: BLE001
            pass
    return {}


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
            except Exception:  # noqa: BLE001
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
            user_group_fallback_enabled=self.plugin_config.notifications.user_group_fallback_enabled,
            user_group_fallback_text=self.plugin_config.notifications.user_group_fallback_text,
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
            client=self.napcat,
        )
        self.auth = AuthorizationService(
            config=self.plugin_config,
            dashboard_username_getter=build_dashboard_username_getter(context),
        )
        self.diagnostics = HandlerDiagnostics(self.context)

        self._initialized = False

    # ------------------------------------------------------------------
    # AstrBot lifecycle
    # ------------------------------------------------------------------
    async def initialize(self) -> None:
        """Awaited by AstrBot before the plugin is considered loaded.

        Startup work is deliberately NOT a fire-and-forget task from ``__init__``: AstrBot
        awaits this hook, so the SQLite schema, the cleanup pass and the outbox worker are
        ready before the first message is dispatched, and failures are visible at load time.
        """
        if self._initialized:
            return
        await self.db.init()
        await self.approval_service.clean_expired()
        keep_days = int(self.plugin_config.security.keep_audit_days)
        if keep_days > 0:
            try:
                await self.audit_service.cleanup_old_logs(keep_days)
            except Exception as e:  # noqa: BLE001 - retention must not block startup
                logger.warning("Audit log retention cleanup failed: %s", e)
        await self.approval_service.sync_reviewers_from_db()
        if self.plugin_config.notifications.retry_enabled:
            self.notification_service.start_worker()
        self._register_web_routes()
        self._initialized = True
        self._report_moderation_precedence()
        logger.info("GroupApprovalPlugin v0.1.1 initialized successfully.")

    @filter.on_astrbot_loaded()
    async def _check_moderation_after_startup(self) -> None:
        self._report_moderation_precedence()

    def _report_moderation_precedence(self) -> None:
        """Log the real, registry-verified moderation precedence (never assume it)."""
        try:
            report = self.diagnostics.diagnose_handlers()
        except Exception:  # noqa: BLE001 - diagnostics must never break startup
            logger.exception("Handler diagnostics failed")
            return
        status = report.get("moderation_precedence")
        message = report.get("message", "")
        if status in {"CONFLICT", "DISABLED"}:
            logger.error("内容审核优先级未满足安全要求：%s %s", message, report.get("recommendations", ""))
        elif status == "VERIFIED":
            logger.info("内容审核优先级已验证：%s", message)
        elif status == "UNVERIFIED":
            logger.warning("内容审核优先级无法验证：%s", message)
        else:
            logger.warning("未检测到 GroupGuardian：%s", message)

    async def terminate(self) -> None:
        """Clean up background workers and close database connections upon unloading."""
        logger.info("Terminating GroupApprovalPlugin...")
        try:
            await self.notification_service.stop_worker()
            await self.db.close()
            logger.info("GroupApprovalPlugin terminated cleanly.")
        except Exception as e:  # noqa: BLE001 - unload must not raise
            logger.error("Error during GroupApprovalPlugin termination: %s", e)

    # ------------------------------------------------------------------
    # Web routes
    # ------------------------------------------------------------------
    def _register_web_routes(self) -> None:
        """Register routes with AstrBot's plugin web framework (4.26+)."""
        if self.context is None or not hasattr(self.context, "register_web_api"):
            return

        def wrap(fn: Any) -> Any:
            async def handler(*args: Any, **kwargs: Any) -> Any:
                res = await fn(*args, **kwargs)
                return json_response(res)

            return handler

        routes = [
            (f"/{PLUGIN_NAME}/bots", self._web_bots, ["GET"], "List connected bots"),
            (f"/{PLUGIN_NAME}/dashboard", self._web_dashboard, ["GET"], "Dashboard metrics"),
            (f"/{PLUGIN_NAME}/diagnostics", self._web_diagnostics, ["GET"], "Handler precedence diagnostics"),
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
            (f"/{PLUGIN_NAME}/audit_logs/export", self._web_export_audit_logs, ["GET"], "Export audit logs"),
        ]

        for path, handler_fn, methods, desc in routes:
            try:
                self.context.register_web_api(path, wrap(handler_fn), methods, desc)
            except Exception as e:  # noqa: BLE001 - missing route must not break loading
                logger.warning("Could not register web route %s: %s", path, e)

    # --- Web Route Handlers with Authentication & Server-Side Authorization ---
    async def _web_bots(self) -> dict[str, Any]:
        operator = self.auth.get_authenticated_user(request)
        if not operator:
            return {"status": "error", "retcode": 401, "message": "Unauthorized: Dashboard login required"}
        return await self.web_api.list_bots(context=self.context)

    async def _web_dashboard(self) -> dict[str, Any]:
        operator = self.auth.get_authenticated_user(request)
        if not operator:
            return {"status": "error", "retcode": 401, "message": "Unauthorized: Dashboard login required"}
        return await self.web_api.get_dashboard_stats(_get_query_param(request, "bot_self_id"))

    async def _web_diagnostics(self) -> dict[str, Any]:
        operator = self.auth.get_authenticated_user(request)
        if not operator:
            return {"status": "error", "retcode": 401, "message": "Unauthorized: Dashboard login required"}
        return {"status": "ok", "retcode": 0, "data": self.diagnostics.diagnose_handlers()}

    async def _web_applications(self) -> dict[str, Any]:
        operator = self.auth.get_authenticated_user(request)
        if not operator:
            return {"status": "error", "retcode": 401, "message": "Unauthorized: Dashboard login required"}
        return await self.web_api.list_applications(
            group_id=_get_query_param(request, "group_id"),
            status=_get_query_param(request, "status"),
            user_id=_get_query_param(request, "user_id"),
            bot_self_id=_get_query_param(request, "bot_self_id"),
        )

    async def _web_approve(self) -> dict[str, Any]:
        return await self._authorized_mutation("approve", self.web_api.approve_application)

    async def _web_reject(self) -> dict[str, Any]:
        return await self._authorized_mutation("reject", self.web_api.reject_application)

    async def _web_manual_grant(self) -> dict[str, Any]:
        data = await _get_request_json(request)
        is_level_exempt = parse_bool(data.get("is_level_exempt", False))
        return await self._authorized_mutation(
            "grant", self.web_api.manual_grant, is_level_exempt=is_level_exempt
        )

    async def _web_revoke_grant(self) -> dict[str, Any]:
        return await self._authorized_mutation("revoke", self.web_api.revoke_grant)

    async def _web_ban(self) -> dict[str, Any]:
        return await self._authorized_mutation("ban", self.web_api.ban_user)

    async def _web_unban(self) -> dict[str, Any]:
        return await self._authorized_mutation("unban", self.web_api.unban_user)

    async def _web_update_group(self) -> dict[str, Any]:
        return await self._authorized_mutation("update_group", self.web_api.update_group_policy)

    async def _authorized_mutation(
        self,
        action: str,
        method: Any,
        *,
        is_level_exempt: bool = False,
    ) -> dict[str, Any]:
        operator = self.auth.get_authenticated_user(request)
        if not operator:
            return {"status": "error", "retcode": 401, "message": "Unauthorized: Dashboard login required"}
        ok, role, reason = await self.auth.authorize_action(
            operator, action, is_level_exempt=is_level_exempt
        )
        if not ok:
            return {"status": "error", "retcode": 403, "message": f"Forbidden: {reason}"}
        data = await _get_request_json(request)
        return await method(data, actor_id=operator, actor_role=role)

    async def _web_users(self) -> dict[str, Any]:
        operator = self.auth.get_authenticated_user(request)
        if not operator:
            return {"status": "error", "retcode": 401, "message": "Unauthorized: Dashboard login required"}
        return await self.web_api.list_users(
            group_id=_get_query_param(request, "group_id"),
            user_id=_get_query_param(request, "user_id"),
            status=_get_query_param(request, "status"),
            bot_self_id=_get_query_param(request, "bot_self_id"),
        )

    async def _web_groups(self) -> dict[str, Any]:
        operator = self.auth.get_authenticated_user(request)
        if not operator:
            return {"status": "error", "retcode": 401, "message": "Unauthorized: Dashboard login required"}
        return await self.web_api.list_groups(bot_self_id=_get_query_param(request, "bot_self_id"))

    async def _web_audit_logs(self) -> dict[str, Any]:
        operator = self.auth.get_authenticated_user(request)
        if not operator:
            return {"status": "error", "retcode": 401, "message": "Unauthorized: Dashboard login required"}
        return await self.web_api.list_audit_logs(
            group_id=_get_query_param(request, "group_id"),
            user_id=_get_query_param(request, "user_id"),
            action=_get_query_param(request, "action"),
            bot_self_id=_get_query_param(request, "bot_self_id"),
        )

    async def _web_export_audit_logs(self) -> dict[str, Any]:
        operator = self.auth.get_authenticated_user(request)
        if not operator:
            return {"status": "error", "retcode": 401, "message": "Unauthorized: Dashboard login required"}
        ok, role, reason = await self.auth.authorize_action(operator, "export_logs")
        if not ok:
            return {"status": "error", "retcode": 403, "message": f"Forbidden: {reason}"}
        # Return the WebApiHandler payload directly: it already contains
        # status/retcode/format/filename/data and its own 400 response for invalid input.
        return await self.web_api.export_audit_logs(
            format_type=str(_get_query_param(request, "format", "json")).strip().lower(),
            bot_self_id=_get_query_param(request, "bot_self_id"),
            group_id=_get_query_param(request, "group_id"),
            action=_get_query_param(request, "action"),
        )

    # ------------------------------------------------------------------
    # Trusted admission dispatcher (priority GATE_PRIORITY = 50)
    # ------------------------------------------------------------------
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.ALL, priority=GATE_PRIORITY)
    async def on_message_gate(self, event: AstrMessageEvent) -> None:
        """Single trusted admission entry point.

        Ordering: moderation (patched GroupGuardian, priority > 50) -> this gate (50) ->
        ordinary business plugins (0). Reply-producing admission commands are executed HERE
        and the event is stopped, so they can never leak to other plugins or the LLM.
        """
        if not _is_onebot_event(event):
            return
        msg_obj = getattr(event, "message_obj", None)
        if msg_obj is None:
            return

        # Only chat messages are admission-relevant; notices/requests carry no message text.
        if not _is_chat_message(event, msg_obj):
            return
        group_id = _group_id(event)
        is_private = bool(getattr(event, "is_private_chat", lambda: False)())
        if not group_id and not is_private:
            return

        bot_self_id = _bot_self_id(event)
        user_id = str(event.get_sender_id() or "").strip()
        message_str = getattr(event, "message_str", "") or ""
        is_command = bool(getattr(event, "is_at_or_wake_command", False))

        # Registry-less OneBot clients can learn short-lived routing candidates from events.
        try:
            await self.napcat.remember_event_identity(event)
        except Exception:  # noqa: BLE001 - identity attestation must not break the gate
            logger.debug("Could not attest bot identity from event", exc_info=True)

        is_group_admin = False
        sender_obj = getattr(msg_obj, "sender", None)
        if sender_obj is not None and getattr(sender_obj, "role", "") in ("owner", "admin"):
            is_group_admin = True

        is_at_bot = False
        for comp in getattr(msg_obj, "message", None) or []:
            if getattr(comp, "type", "") == "at" and str(getattr(comp, "qq", "")) == bot_self_id:
                is_at_bot = True
                break

        decision = await self.gate.decide(
            bot_self_id=bot_self_id,
            group_id=group_id,
            user_id=user_id,
            message_str=message_str,
            is_command=is_command,
            is_at_bot=is_at_bot,
            is_group_admin=is_group_admin,
        )

        if decision.kind is AdmissionKind.BLOCK:
            if self.plugin_config.security.log_denied_messages:
                logger.info(
                    "Admission blocked: bot=%s group=%s user=%s text=%r",
                    bot_self_id,
                    group_id,
                    user_id,
                    message_str[:100],
                )
            event.stop_event()
            if decision.notice:
                yield event.plain_result(decision.notice)
            return

        if decision.kind is AdmissionKind.USER_COMMAND:
            reply = await self._execute_user_command(
                decision.command, bot_self_id, group_id or "", user_id, message_str
            )
            event.stop_event()
            yield event.plain_result(reply)
            return

        if decision.kind is AdmissionKind.ADMIN_COMMAND:
            reply = await self.dispatcher.handle_admin_command(
                bot_self_id=bot_self_id,
                sender_id=user_id,
                group_id=group_id,
                args=decision.args,
            )
            event.stop_event()
            yield event.plain_result(reply)
            return

        # The LivingMemory compatibility patch defers its pre-handler filter side effect.
        # Only a successful group admission may release the queued callback.
        if group_id and self.plugin_config.enabled:
            capture = event.get_extra(LIVINGMEMORY_CAPTURE_KEY)
            if callable(capture):
                event.set_extra(LIVINGMEMORY_CAPTURE_KEY, None)
                try:
                    capture()
                except Exception:
                    logger.exception("Could not schedule admitted LivingMemory capture")

    async def _execute_user_command(
        self,
        command: str,
        bot_self_id: str,
        group_id: str,
        user_id: str,
        message_str: str,
    ) -> str:
        """Execute a classified admission command via the command dispatcher."""
        parts = str(message_str or "").strip().split()
        reason = " ".join(parts[1:]).strip() if len(parts) > 1 else ""
        if command == "申请使用":
            return await self.dispatcher.handle_apply(bot_self_id, group_id, user_id, reason)
        if command == "我的权限":
            return await self.dispatcher.handle_my_status(bot_self_id, group_id, user_id)
        if command == "申请状态":
            return await self.dispatcher.handle_application_status(bot_self_id, group_id, user_id)
        if command == "取消申请":
            return await self.dispatcher.handle_cancel_application(bot_self_id, group_id, user_id)
        if command == "使用规则":
            return await self.dispatcher.handle_rules(bot_self_id, group_id)
        return "未知的准入指令"

    # ------------------------------------------------------------------
    # Command handlers (defense in depth; the gate executes commands first)
    # ------------------------------------------------------------------
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.command("申请使用")
    async def cmd_apply(self, event: AstrMessageEvent) -> None:
        if not _is_onebot_event(event):
            return
        bot_self_id = _bot_self_id(event)
        group_id = _group_id(event) or ""
        user_id = str(event.get_sender_id() or "").strip()
        reason = getattr(event, "message_str", "").replace("/申请使用", "").replace("申请使用", "").strip()
        reply = await self.dispatcher.handle_apply(bot_self_id, group_id, user_id, reason)
        event.stop_event()
        yield event.plain_result(reply)

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.command("我的权限")
    async def cmd_my_status(self, event: AstrMessageEvent) -> None:
        if not _is_onebot_event(event):
            return
        reply = await self.dispatcher.handle_my_status(
            _bot_self_id(event), _group_id(event) or "", str(event.get_sender_id() or "").strip()
        )
        event.stop_event()
        yield event.plain_result(reply)

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.command("申请状态")
    async def cmd_application_status(self, event: AstrMessageEvent) -> None:
        if not _is_onebot_event(event):
            return
        reply = await self.dispatcher.handle_application_status(
            _bot_self_id(event), _group_id(event) or "", str(event.get_sender_id() or "").strip()
        )
        event.stop_event()
        yield event.plain_result(reply)

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.command("取消申请")
    async def cmd_cancel_application(self, event: AstrMessageEvent) -> None:
        if not _is_onebot_event(event):
            return
        reply = await self.dispatcher.handle_cancel_application(
            _bot_self_id(event), _group_id(event) or "", str(event.get_sender_id() or "").strip()
        )
        event.stop_event()
        yield event.plain_result(reply)

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.command("使用规则")
    async def cmd_rules(self, event: AstrMessageEvent) -> None:
        if not _is_onebot_event(event):
            return
        reply = await self.dispatcher.handle_rules(_bot_self_id(event), _group_id(event) or "")
        event.stop_event()
        yield event.plain_result(reply)

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.command("ga")
    async def cmd_ga(self, event: AstrMessageEvent) -> None:
        if not _is_onebot_event(event):
            return
        msg = getattr(event, "message_str", "").strip()
        parts = [p for p in msg.split() if p]
        args = parts[1:] if len(parts) > 1 else []
        reply = await self.dispatcher.handle_admin_command(
            bot_self_id=_bot_self_id(event),
            sender_id=str(event.get_sender_id() or "").strip(),
            group_id=_group_id(event),
            args=args,
        )
        event.stop_event()
        yield event.plain_result(reply)
