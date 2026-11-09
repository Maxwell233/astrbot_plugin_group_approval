"""Read-only handler diagnostics: verify real AstrBot handler precedence.

Verified AstrBot 4.29.4 facts used here (see COMPATIBILITY.md):
* The global registry is ``astrbot.core.star.star_handler.star_handlers_registry``.
  (There is no ``astrbot.core.star.star_handlers_registry`` module.)
* Priority is stored in ``handler.extras_configs["priority"]`` (default 0); there is no
  ``handler.priority`` attribute.
* The registry iterates handlers in real dispatch order (descending priority, stable
  registration order for ties), because ``StarHandlerRegistry.append`` sorts with
  ``key=lambda h: -h.extras_configs["priority"]``.

This service only *reports* reality. It never mutates another plugin's handler state.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("astrbot_plugin_group_approval.diagnostics")

GATE_PRIORITY = 50
MODERATION_MIN_PRIORITY = GATE_PRIORITY + 1
MODERATION_RECOMMENDED_PRIORITY = 100

_PLUGIN_MODULE_HINT = "astrbot_plugin_group_approval"
_MODERATION_MODULE_HINT = "group_guardian"
_MODERATION_HANDLER_NAMES = ("_handle_message",)


def _load_registry() -> Any | None:
    """Import the real AstrBot registry, or return None when unavailable."""
    try:
        from astrbot.core.star.star_handler import star_handlers_registry

        return star_handlers_registry
    except Exception:  # noqa: BLE001 - older AstrBot or import failure
        return None


class HandlerDiagnostics:
    """Inspects the real StarHandler registry and reports moderation precedence."""

    def __init__(self, context: Any = None):
        self.context = context

    def _iter_handlers(self) -> list[Any] | None:
        registry = _load_registry()
        if registry is None:
            return None
        try:
            handlers = list(registry)
        except TypeError:
            handlers = list(getattr(registry, "_handlers", []) or [])
        return handlers

    @staticmethod
    def _priority_of(handler: Any) -> int:
        extras = getattr(handler, "extras_configs", None) or {}
        try:
            return int(extras.get("priority", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def diagnose_handlers(self) -> dict[str, Any]:
        """Return a factual report about handler precedence.

        ``moderation_precedence`` values:
          - ``VERIFIED``: a GroupGuardian message handler runs before this gate.
          - ``CONFLICT``: GroupGuardian is installed but would be pre-empted by the gate.
          - ``NOT_INSTALLED``: no GroupGuardian handlers found.
          - ``UNVERIFIED``: the real registry could not be imported (framework too old).
        """
        handlers = self._iter_handlers()
        if handlers is None:
            return {
                "moderation_precedence": "UNVERIFIED",
                "gate_priority": GATE_PRIORITY,
                "moderation_priority": None,
                "message": "无法导入 AstrBot 真实 Handler 注册表，无法验证内容审核与准入闸门的执行顺序。",
                "recommendations": "确认 AstrBot 版本 >= 4.26.0（本插件的最低支持版本）。",
                "handlers": [],
            }

        records: list[dict[str, Any]] = []
        gate_priority: int | None = None
        moderation: list[dict[str, Any]] = []

        for handler in handlers:
            module = str(getattr(handler, "handler_module_path", "") or "")
            name = str(getattr(handler, "handler_name", "") or "")
            priority = self._priority_of(handler)
            records.append(
                {
                    "name": name,
                    "module": module,
                    "priority": priority,
                }
            )
            if _PLUGIN_MODULE_HINT in module and name == "on_message_gate":
                gate_priority = priority
            if _MODERATION_MODULE_HINT in module.lower():
                moderation.append({"name": name, "priority": priority})

        effective_gate_priority = gate_priority if gate_priority is not None else GATE_PRIORITY

        if not moderation:
            status = "NOT_INSTALLED"
            moderation_priority = None
            message = "未在真实注册表中发现 GroupGuardian 审核 Handler。"
            recommendations = (
                "若需要群内容安全审核，请安装 GroupGuardian 并应用兼容补丁 "
                "(patches/group_guardian_priority.patch)。"
            )
        else:
            moderation_priority = max(item["priority"] for item in moderation)
            if moderation_priority > effective_gate_priority:
                status = "VERIFIED"
                message = (
                    f"已验证：GroupGuardian 审核 Handler 实际优先级 {moderation_priority} "
                    f"> 准入闸门 {effective_gate_priority}，未授权用户的违规消息会先被审核。"
                )
                recommendations = "当前 Handler 顺序符合安全要求，无需额外配置。"
            else:
                status = "CONFLICT"
                message = (
                    f"安全冲突：GroupGuardian 审核 Handler 实际优先级 {moderation_priority} "
                    f"<= 准入闸门 {effective_gate_priority}。"
                    "未授权用户的违规消息会在被审核前遭到拦截。"
                )
                recommendations = (
                    "请对 GroupGuardian 应用 patches/group_guardian_priority.patch"
                    f"（将主审核 Handler 提升至 priority={MODERATION_RECOMMENDED_PRIORITY}）。"
                )

        return {
            "moderation_precedence": status,
            "gate_priority": effective_gate_priority,
            "moderation_priority": moderation_priority,
            "recommended_moderation_priority": MODERATION_RECOMMENDED_PRIORITY,
            "message": message,
            "recommendations": recommendations,
            "handlers": records,
        }
