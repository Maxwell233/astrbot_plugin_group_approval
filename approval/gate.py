"""Event admission gate: classify events and enforce access policy before business handlers.

The gate is the single trusted admission dispatcher for the plugin. It does two things:

1. **Classifies admission commands by exact token match** (``申请使用``, ``我的权限``,
   ``申请状态``, ``取消申请``, ``使用规则``, and the admin command ``ga``). It never guesses
   with substring/prefix matching, and it only treats a message as a command when AstrBot's
   waking stage set ``is_at_or_wake_command`` (i.e. the configured global wake prefix was
   actually used). The caller (``main.py:on_message_gate``) executes the command inside the
   same handler and stops the event, so an admission command can never fall through to
   unrelated plugins or the LLM.
2. **Enforces access control** for all other messages: unauthorized users are denied before
   any lower-priority (business) handler runs.

Handler ordering requirement: GroupGuardian's moderation handler runs at the AstrBot default
priority 0 upstream, so this gate at ``GATE_PRIORITY`` would pre-empt it. Unauthorized users
MUST still be moderated. The supported deployment therefore requires the GroupGuardian
compatibility patch (``patches/group_guardian_priority.patch``) which raises its moderation
handler above ``GATE_PRIORITY``. ``HandlerDiagnostics`` verifies the real registry and the
plugin reports unverified precedence instead of assuming it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

from .access_service import AccessService
from .config import PluginConfig
from .models import AccessDecision

logger = logging.getLogger("astrbot_plugin_group_approval.gate")

# The gate must run after moderation and before ordinary business handlers (priority 0).
GATE_PRIORITY = 50
# Minimum priority the moderation handler must have to run BEFORE this gate.
MODERATION_MIN_PRIORITY = GATE_PRIORITY + 1
# Priority recommended for the GroupGuardian compatibility patch.
MODERATION_RECOMMENDED_PRIORITY = 100

# Exact admission command tokens (after AstrBot stripped the global wake prefix).
USER_COMMANDS: tuple[str, ...] = ("申请使用", "我的权限", "申请状态", "取消申请", "使用规则")
ADMIN_COMMAND = "ga"


class AdmissionKind(str, Enum):
    """What the trusted dispatcher should do with an event."""

    ALLOW = "allow"  # authorized: continue to normal business handlers
    BLOCK = "block"  # unauthorized: stop_event (+ optional notice)
    USER_COMMAND = "user_command"  # handled by the gate itself
    ADMIN_COMMAND = "admin_command"  # handled by the gate itself (authorization inside)


@dataclass
class AdmissionDecision:
    kind: AdmissionKind
    command: str = ""
    args: list[str] = field(default_factory=list)
    notice: str | None = None


class AdmissionGate:
    """Classifies message events and decides admission (no side effects on its own)."""

    def __init__(self, access_service: AccessService, config: PluginConfig):
        self.access = access_service
        self.config = config

    # ------------------------------------------------------------------
    # Command parsing
    # ------------------------------------------------------------------
    def parse_command(self, message_str: str, is_command: bool) -> tuple[str, list[str]] | None:
        """Return ``(command, args)`` for an exact admission command, else ``None``.

        ``is_command`` must be AstrBot's own ``event.is_at_or_wake_command`` result, so the
        deployed ``wake_prefix`` configuration is respected. Matching is done on the first
        whitespace token, exactly - "garbage" or "今日运势" never match ``ga``.
        """
        if not is_command:
            return None
        parts = str(message_str or "").strip().split()
        if not parts:
            return None
        token = parts[0]
        if token in USER_COMMANDS:
            return token, parts[1:]
        if token == ADMIN_COMMAND:
            return ADMIN_COMMAND, parts[1:]
        return None

    def parse_admin_args(self, message_str: str) -> list[str]:
        """Return the argument list of an already-classified admin command."""
        parts = str(message_str or "").strip().split()
        return parts[1:] if len(parts) > 1 else []

    # ------------------------------------------------------------------
    # Decision
    # ------------------------------------------------------------------
    async def decide(
        self,
        *,
        bot_self_id: str,
        group_id: str | None,
        user_id: str,
        message_str: str,
        is_command: bool = False,
        is_at_bot: bool = False,
        is_group_admin: bool = False,
    ) -> AdmissionDecision:
        """Classify one event. Pure: performs reads only, never stops the event itself."""
        if not self.config.enabled:
            return AdmissionDecision(AdmissionKind.ALLOW)

        b_id = str(bot_self_id or "").strip()
        u_id = str(user_id or "").strip()
        g_id = str(group_id or "").strip()

        # Fail closed: without a verified bot identity we cannot evaluate per-bot policy.
        if not b_id:
            return AdmissionDecision(
                AdmissionKind.BLOCK,
                notice="【权限拦截】无法确认当前 Bot 身份，已拒绝处理该消息。",
            )

        command = self.parse_command(message_str, is_command)

        # Private chat: no group policy. Admin commands are still routed for authorization;
        # admission commands are routed so the user gets a meaningful reply.
        if not g_id:
            if command is not None:
                if command[0] == ADMIN_COMMAND:
                    return AdmissionDecision(AdmissionKind.ADMIN_COMMAND, ADMIN_COMMAND, command[1])
                return AdmissionDecision(AdmissionKind.USER_COMMAND, command[0], command[1])
            if not self.config.private_chat.enabled_for_normal_users:
                if not self.config.is_owner(u_id) and not self.config.is_reviewer(u_id):
                    return AdmissionDecision(
                        AdmissionKind.BLOCK,
                        notice="抱歉，Bot 未开启私聊普通对话功能。",
                    )
            return AdmissionDecision(AdmissionKind.ALLOW)

        # Group chat: admission commands are executed by the trusted dispatcher, never
        # forwarded to lower-priority handlers.
        if command is not None:
            if command[0] == ADMIN_COMMAND:
                return AdmissionDecision(AdmissionKind.ADMIN_COMMAND, ADMIN_COMMAND, command[1])
            return AdmissionDecision(AdmissionKind.USER_COMMAND, command[0], command[1])

        # Ordinary business / AI path: enforce authorization.
        decision: AccessDecision = await self.access.check_access(
            bot_id=b_id,
            group_id=g_id,
            user_id=u_id,
            is_group_admin=is_group_admin,
        )
        if decision.allowed:
            return AdmissionDecision(AdmissionKind.ALLOW)

        notice: str | None = None
        if self._should_notice(message_str, is_at_bot):
            notice = self._build_notice(decision)
        return AdmissionDecision(AdmissionKind.BLOCK, notice=notice)

    # ------------------------------------------------------------------
    # Notice wording (no authorization meaning)
    # ------------------------------------------------------------------
    def _should_notice(self, message_str: str, is_at_bot: bool) -> bool:
        if not self.config.security.command_prefix_required_for_rejection_notice:
            return True
        if is_at_bot:
            return True
        return str(message_str or "").strip().startswith(("/", "#", "!", "！", "、"))

    def _build_notice(self, decision: AccessDecision) -> str:
        if "BLACKLISTED" in decision.reason:
            return "【权限拦截】您已被列入黑名单，无法使用群内 Bot 功能。"
        if decision.reason == "LEVEL_BELOW_MINIMUM":
            return (
                f"【权限拦截】由于您的群等级低于本群门槛 (LV{decision.minimum_level})，"
                f"使用权限已被暂时冻结。当等级达到门槛后将自动恢复。"
            )
        if decision.reason == "GRANT_EXPIRED":
            return "【权限拦截】您在当前群的 Bot 使用权限已到期，请发送「/申请使用」重新申请。"
        if decision.reason == "INVALID_IDENTITY":
            return "【权限拦截】无法确认会话身份，已拒绝处理。"
        return (
            f"【权限拦截】您尚未获得当前群的 Bot 使用权限。\n"
            f"当前群准入门槛为 LV{decision.minimum_level}。达到等级要求后，请发送「/申请使用」提交权限申请。"
        )
