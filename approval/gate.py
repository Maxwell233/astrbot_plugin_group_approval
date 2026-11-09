"""Event admission gate enforcing access policies before ordinary plugins and LLM pipeline."""

from __future__ import annotations

import logging

from .access_service import AccessService
from .config import PluginConfig
from .models import AccessDecision

logger = logging.getLogger("astrbot_plugin_group_approval.gate")

# Prefixes or keywords recognized as Group Approval admission commands
GA_COMMANDS = (
    "/申请使用",
    "申请使用",
    "/我的权限",
    "我的权限",
    "/申请状态",
    "申请状态",
    "/取消申请",
    "取消申请",
    "/使用规则",
    "使用规则",
    "/ga",
    "ga",
)


class AdmissionGate:
    """Evaluates message events, stopping unauthorized execution while permitting audit and admin commands."""

    def __init__(self, access_service: AccessService, config: PluginConfig):
        self.access = access_service
        self.config = config

    def is_ga_command(self, message_str: str) -> bool:
        """Check if message begins with a group approval management command."""
        s = message_str.strip()
        for cmd in GA_COMMANDS:
            if s == cmd or s.startswith(cmd + " "):
                return True
        return False

    def is_bot_invoked(self, message_str: str, is_at_bot: bool) -> bool:
        """Check whether the message was intentionally directed at the bot."""
        if is_at_bot:
            return True
        s = message_str.strip()
        # Common command prefixes
        if s.startswith(("/", "#", "!", "！", "、")):
            return True
        # Explicit bot trigger words
        for kw in ("今日运势", "jrys", "抽小猪", "今日小猪", "rollpig", "塔罗", "占卜"):
            if s == kw or s.startswith(kw):
                return True
        return False

    async def should_intercept(
        self,
        bot_self_id: str,
        group_id: str | None,
        user_id: str,
        message_str: str,
        is_at_bot: bool = False,
        is_group_admin: bool = False,
    ) -> tuple[bool, str | None]:
        """Determine whether the message event should be stopped.

        Returns:
            (intercept: bool, notice_message: Optional[str])
        """
        # If plugin globally disabled, do not intercept
        if not self.config.enabled:
            return False, None

        msg_clean = message_str.strip()

        # Handle Private Messages
        if not group_id:
            # Check if this is an admin command in private chat
            if msg_clean.startswith(("/ga", "ga")):
                return False, None

            # Normal user private chat policy
            if not self.config.private_chat.enabled_for_normal_users:
                # Disallow normal private chat if configured
                if not self.config.is_owner(user_id) and not self.config.is_reviewer(user_id):
                    return True, "抱歉，Bot 未开启私聊普通对话功能。"
            return False, None

        # Allow Group Approval user and admin commands to pass through to their handlers
        if self.is_ga_command(msg_clean):
            return False, None

        # Check access permission for ordinary business and AI
        decision: AccessDecision = await self.access.check_access(
            bot_id=bot_self_id,
            group_id=group_id,
            user_id=user_id,
            is_group_admin=is_group_admin,
        )

        if decision.allowed:
            return False, None

        # Intercept event for unauthorized user
        # Determine whether to emit a polite notice
        should_notice = False
        if self.is_bot_invoked(msg_clean, is_at_bot) or not self.config.security.command_prefix_required_for_rejection_notice:
            should_notice = True

        notice_text = None
        if should_notice:
            if "BLACKLISTED" in decision.reason:
                notice_text = "【权限拦截】您已被列入黑名单，无法使用群内 Bot 功能。"
            elif decision.reason == "LEVEL_BELOW_MINIMUM":
                notice_text = (
                    f"【权限拦截】由于您的群等级低于本群门槛 (LV{decision.minimum_level})，"
                    f"使用权限已被暂时冻结。当等级达到门槛后将自动恢复。"
                )
            elif decision.reason == "GRANT_EXPIRED":
                notice_text = "【权限拦截】您在当前群的 Bot 使用权限已到期，请发送「/申请使用」重新申请。"
            else:
                notice_text = (
                    f"【权限拦截】您尚未获得当前群的 Bot 使用权限。\n"
                    f"当前群准入门槛为 LV{decision.minimum_level}。达到等级要求后，请发送「/申请使用」提交权限申请。"
                )

        return True, notice_text
