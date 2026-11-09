"""Read-only inspection of the live AstrBot adapter handler registry."""

from __future__ import annotations

from typing import Any

GATE_PRIORITY = 50
MODERATION_MIN_PRIORITY = GATE_PRIORITY + 1
MODERATION_RECOMMENDED_PRIORITY = 100

_APPROVAL = "astrbot_plugin_group_approval"
_GUARDIAN = "astrbot_plugin_group_guardian"


class HandlerDiagnostics:
    """Report registration order independently of per-group moderation settings."""

    def __init__(self, context: Any = None):
        self.context = context

    def diagnose_handlers(self) -> dict[str, Any]:
        try:
            from astrbot.core.star.filter.event_message_type import EventMessageType, EventMessageTypeFilter
            from astrbot.core.star.filter.platform_adapter_type import PlatformAdapterType, PlatformAdapterTypeFilter
            from astrbot.core.star.star import star_map
            from astrbot.core.star.star_handler import EventType, star_handlers_registry
        except ImportError:
            return self._report("UNVERIFIED", "无法读取 AstrBot Handler 注册表。")

        def is_group_onebot_listener(handler: Any) -> bool:
            filters = handler.event_filters
            return (
                len(filters) == 2
                and any(isinstance(f, EventMessageTypeFilter) and f.event_message_type & EventMessageType.GROUP_MESSAGE
                        for f in filters)
                and any(isinstance(f, PlatformAdapterTypeFilter) and f.platform_type == PlatformAdapterType.AIOCQHTTP
                        for f in filters)
            )

        def find(plugin_name: str, method: str) -> list[Any]:
            return [
                h for h in star_handlers_registry
                if h.event_type == EventType.AdapterMessageEvent
                and h.handler_name == method
                and h.handler_full_name == f"{h.handler_module_path}_{method}"
                and getattr(star_map.get(h.handler_module_path), "name", None) == plugin_name
                and (plugin_name != _GUARDIAN or h.handler_module_path.endswith(".main"))
            ]

        installed = [plugin for plugin in star_map.values() if plugin.name == _GUARDIAN]
        if not installed:
            return self._report("NOT_INSTALLED", "AstrBot 未加载 GroupGuardian，无法提供审核先行。")
        if not any(plugin.activated for plugin in installed):
            return self._report("DISABLED", "GroupGuardian 已停用，无法提供审核先行。")

        gates = find(_APPROVAL, "on_message_gate")
        moderators = find(_GUARDIAN, "_handle_message")
        if len(gates) != 1 or len(moderators) != 1:
            return self._report("UNVERIFIED", "无法唯一定位准入闸门与 GroupGuardian 主审核入口。")
        gate, moderator = gates[0], moderators[0]
        if not all(is_group_onebot_listener(h) for h in (gate, moderator)):
            return self._report("UNVERIFIED", "审核或准入入口的群消息平台过滤器与已核对版本不同。")

        handlers = star_handlers_registry.get_handlers_by_event_type(EventType.AdapterMessageEvent)
        if moderator not in handlers or gate not in handlers:
            return self._report("DISABLED", "审核或准入入口未处于启用的消息调度列表。")

        gate_position = handlers.index(gate)
        moderation_position = handlers.index(moderator)
        gate_priority = gate.extras_configs.get("priority", 0)
        moderation_priority = moderator.extras_configs.get("priority", 0)
        order = [
            {
                "name": h.handler_name,
                "module": h.handler_module_path,
                "priority": h.extras_configs.get("priority", 0),
                "position": i,
            }
            for i, h in enumerate(handlers)
        ]
        verified = (
            isinstance(moderation_priority, int)
            and isinstance(gate_priority, int)
            and moderation_priority > gate_priority
            and moderation_position < gate_position
        )
        report = self._report(
            "VERIFIED" if verified else "CONFLICT",
            "已验证注册表中主审核入口排在准入闸门之前。"
            if verified else "阻断级配置错误：无法保证主审核先于准入闸门。请暂停受影响 Bot 的群消息接入。",
            gate_priority,
            moderation_priority,
        )
        report.update(
            gate_position=gate_position,
            moderation_position=moderation_position,
            handlers=order,
            other_handlers_before_gate=[h for h in order[:gate_position] if h["position"] != moderation_position],
        )
        return report

    @staticmethod
    def _report(status: str, message: str, gate_priority=None, moderation_priority=None) -> dict[str, Any]:
        return {
            "moderation_precedence": status,
            "gate_priority": gate_priority,
            "moderation_priority": moderation_priority,
            "recommended_moderation_priority": MODERATION_RECOMMENDED_PRIORITY,
            "message": message,
            "recommendations": (
                "核对每群审核开关、Bot 群管理权限、会话插件过滤和闸门前的其他 Handler。"
                if status == "VERIFIED" else
                "核对两插件均已启用，应用 patches/group_guardian_priority.patch 并重载后重新诊断。"
            ),
            "handlers": [],
        }
