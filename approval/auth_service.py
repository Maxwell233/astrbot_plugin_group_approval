"""Authentication and server-side authorization service for WebUI operations."""

from __future__ import annotations

import logging
from typing import Any

from .config import PluginConfig

logger = logging.getLogger("astrbot_plugin_group_approval.auth")


class AuthorizationService:
    """Authenticates dashboard requests and enforces role-based permissions server-side."""

    def __init__(self, config: PluginConfig, is_reviewer_fn: Any = None):
        self.config = config
        self._is_reviewer_fn = is_reviewer_fn

    def get_authenticated_user(self, req: Any) -> str | None:
        """Extract authenticated username strictly from official dashboard session/context.

        Rejects spoofable client HTTP headers (e.g. X-AstrBot-User, Authorization-User).
        Returns username string if authenticated, or None if unauthenticated.
        """
        if req is None:
            return None

        # 1. AstrBot / Quart request authenticated username attribute
        username = getattr(req, "username", None)
        if username and str(username).strip():
            return str(username).strip()

        # 2. Quart session user
        session = getattr(req, "session", None)
        if isinstance(session, dict):
            s_user = session.get("user") or session.get("username")
            if s_user and str(s_user).strip():
                return str(s_user).strip()

        # 3. Direct request.user object (e.g., Starlette / Quart Auth)
        user_obj = getattr(req, "user", None)
        if user_obj is not None:
            identity = getattr(user_obj, "identity", None) or getattr(user_obj, "username", None) or getattr(user_obj, "name", None)
            if identity and str(identity).strip():
                return str(identity).strip()

        return None

    async def get_user_role(self, username: str) -> str:
        """Determine operator role from system configuration and persistent reviewers."""
        u = str(username).strip()
        if not u:
            return "UNAUTHENTICATED"

        # Check Owner
        if self.config.is_owner(u) or u.lower() in ("admin", "root", "owner"):
            return "OWNER"

        # Check Reviewer (memory config or database)
        if self.config.is_reviewer(u):
            return "REVIEWER"

        if callable(self._is_reviewer_fn):
            try:
                if await self._is_reviewer_fn(u):
                    return "REVIEWER"
            except Exception:
                pass

        return "DASHBOARD_USER"

    async def authorize_action(
        self,
        username: str,
        action: str,
        bot_self_id: str = "",
        group_id: str | None = None,
        is_level_exempt: bool = False,
    ) -> tuple[bool, str, str]:
        """Verify if authenticated user has permission to perform the requested action.

        Returns:
            (authorized: bool, role: str, reason: str)
        """
        u = str(username).strip()
        if not u:
            return False, "UNAUTHENTICATED", "未登录或登录凭证已失效"

        role = await self.get_user_role(u)

        # Read-only actions permitted for any authenticated dashboard user
        read_actions = {
            "view_dashboard",
            "list_bots",
            "list_applications",
            "list_users",
            "list_groups",
            "list_audit_logs",
            "diagnostics",
        }
        if action in read_actions:
            return True, role, ""

        # Level exemption grant is strictly reserved for OWNER
        if action == "grant" and is_level_exempt:
            if role == "OWNER":
                return True, role, ""
            return False, role, "仅机器人主人(OWNER)可以授予免群等级检查特权"

        # Reviewer actions (approve/reject application, revoke standard grant)
        reviewer_actions = {"approve", "reject", "revoke"}
        if action in reviewer_actions:
            if role in ("OWNER", "REVIEWER"):
                return True, role, ""
            return False, role, f"用户 {u} 不是审核员或主人，无权执行 {action} 操作"

        # Owner-exclusive actions (manual grant, blacklist ban/unban, policy updates, log export)
        owner_actions = {"grant", "ban", "unban", "update_group", "export_logs"}
        if action in owner_actions:
            if role == "OWNER":
                return True, role, ""
            return False, role, f"该操作 ({action}) 仅限机器人主人(OWNER)执行"

        return False, role, f"未知或受限操作: {action}"
