"""Read-only handler diagnostics service for AstrBot event priority inspection."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("astrbot_plugin_group_approval.diagnostics")


class HandlerDiagnostics:
    """Inspects StarHandler registration metadata and audits execution precedence."""

    def __init__(self, context: Any = None):
        self.context = context

    def diagnose_handlers(self) -> dict[str, Any]:
        """Scan AstrBot context for registered handlers and verify GroupGuardian precedence.

        AstrBot stores handler priority in handler.extras_configs['priority'] at registration.
        """
        if self.context is None:
            return {
                "group_guardian_status": "UNKNOWN",
                "group_guardian_priority": None,
                "group_approval_priority": 50,
                "message": "AstrBot context not available for runtime handler inspection.",
                "handlers": [],
                "recommendations": "Ensure AstrBot context is initialized before running diagnostics.",
            }

        candidates = []
        for attr in ("star_manager", "star_handlers", "handlers", "event_manager"):
            obj = getattr(self.context, attr, None)
            if obj is not None:
                candidates.append(obj)

        raw_handlers: list[Any] = []
        for target in candidates:
            h_list = getattr(target, "star_handlers", None) or getattr(target, "handlers", None)
            if isinstance(h_list, (list, tuple)):
                raw_handlers.extend(h_list)
            elif isinstance(h_list, dict):
                raw_handlers.extend(h_list.values())
            elif isinstance(target, (list, tuple)):
                raw_handlers.extend(target)

        # Deduplicate
        seen_ids = set()
        unique_handlers = []
        for h in raw_handlers:
            h_id = id(h)
            if h_id not in seen_ids:
                seen_ids.add(h_id)
                unique_handlers.append(h)

        handler_records = []
        gg_found = False
        gg_max_priority = -999

        for h in unique_handlers:
            # Extract priority from official extras_configs first, then getattr fallback
            extras = getattr(h, "extras_configs", {}) or {}
            priority = extras.get("priority")
            if priority is None:
                priority = getattr(h, "priority", 0)
            try:
                priority = int(priority)
            except (ValueError, TypeError):
                priority = 0

            # Extract name and plugin name
            name = str(getattr(h, "name", "") or getattr(h, "__name__", "") or str(h))
            plugin_name = str(
                getattr(h, "plugin_name", "")
                or getattr(h, "star_name", "")
                or getattr(h, "plugin", "")
                or ""
            )
            event_type = str(getattr(h, "event_type", "") or getattr(h, "event_message_type", ""))

            rec = {
                "name": name,
                "plugin_name": plugin_name,
                "priority": priority,
                "event_type": event_type,
            }
            handler_records.append(rec)

            combined_str = f"{name} {plugin_name}".lower()
            if "group_guardian" in combined_str or "groupguardian" in combined_str or "guardian" in combined_str:
                gg_found = True
                if priority > gg_max_priority:
                    gg_max_priority = priority

        # Sort handlers by priority descending (AstrBot dispatch order)
        handler_records.sort(key=lambda x: x["priority"], reverse=True)

        if not gg_found:
            status = "NOT_INSTALLED"
            msg = "GroupGuardian 未安装或未注册Handler。内容审核需依赖其他独立机制。"
            recs = "如需群聊内容安全审核，请安装并配置 astrbot_plugin_group_guardian (建议 priority=100)。"
            gg_prio = None
        elif gg_max_priority >= 100:
            status = "OPTIMAL"
            msg = f"GroupGuardian 审核 Handler 优先级 ({gg_max_priority}) >= 100，优先于准入闸门 (50)。内容审核先于准入拦截，执行顺序安全。"
            recs = "当前 Handler 排序安全，无需额外配置。"
            gg_prio = gg_max_priority
        else:
            status = "WARNING"
            msg = (
                f"安全风险：GroupGuardian 审核 Handler 优先级 ({gg_max_priority}) <= 准入闸门 (50)！"
                "未获授权群员的违规消息可能在到达 GroupGuardian 审核前被准入闸门拦截。"
            )
            recs = (
                "请为 GroupGuardian 应用补丁：在 GroupGuardian 的主审核 Handler 装饰器中显式指定 priority=100："
                "\n@filter.event_message_type(filter.EventMessageType.ALL, priority=100)"
            )
            gg_prio = gg_max_priority

        return {
            "group_guardian_status": status,
            "group_guardian_priority": gg_prio,
            "group_approval_priority": 50,
            "message": msg,
            "handlers": handler_records,
            "recommendations": recs,
        }
