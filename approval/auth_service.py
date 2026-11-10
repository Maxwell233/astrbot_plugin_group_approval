"""Authentication and server-side authorization for WebUI (dashboard) operations.

Identity model (verified against AstrBot 4.29.4 source):

* AstrBot authenticates the request **before** calling a plugin web handler: the JWT (or an
  API key with the ``plugin`` scope) is validated by the route dependency, and the resulting
  identity is exposed as ``request.username`` on the contextvar request proxy. It is the only
  identity signal a plugin gets - there is no ``request.session``, ``request.user`` or role
  table (upstream has a single dashboard account: ``dashboard.username``).
* Therefore dashboard authorization is based on exactly two facts: (1) the request is
  authenticated by AstrBot, and (2) the authenticated username equals the configured
  AstrBot dashboard username. Nothing else.
* QQ identities (plugin Owner/Reviewer QQ ids) are a **separate** identity system and are
  never consulted here: a dashboard username is not a QQ number and must never be mapped to
  QQ privileges. Deleted/unknown identities are denied management operations.
* API-key callers appear as ``api_key:<id>``; they are authenticated but are not a verified
  human dashboard operator, so they are rejected as operators (they cannot be mapped to the
  single dashboard account).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("astrbot_plugin_group_approval.auth")

class AuthorizationService:
    """Authenticates dashboard requests and authorizes actions server-side."""

    def __init__(
        self,
        dashboard_username_getter: Callable[[], str | None] | None = None,
    ):
        self._dashboard_username_getter = dashboard_username_getter

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------
    def get_dashboard_username(self) -> str | None:
        """Resolve the configured AstrBot dashboard username, if available."""
        if not callable(self._dashboard_username_getter):
            return None
        try:
            value = self._dashboard_username_getter()
        except Exception as e:  # noqa: BLE001 - config unavailable => fail closed
            logger.warning("Could not read AstrBot dashboard username: %s", e)
            return None
        if value is None:
            return None
        value = str(value).strip()
        return value or None

    def get_authenticated_user(self, req: Any) -> str | None:
        """Return the AstrBot-authenticated dashboard username, or None.

        Only ``request.username`` is used. It is injected by AstrBot from the validated
        JWT/API-key identity *after* authentication, so it cannot be spoofed by a client
        header. Quart-era attributes (``session``/``user``) do not exist upstream and are
        deliberately not trusted.
        """
        if req is None:
            return None
        username = getattr(req, "username", None)
        if not username:
            return None
        u = str(username).strip()
        if not u:
            return None
        if u.startswith("api_key:"):
            # Authenticated machine credential, not a verified dashboard operator.
            return None
        return u

    async def get_user_role(self, username: str) -> str:
        """Resolve the dashboard role of an authenticated username."""
        u = str(username or "").strip()
        if not u:
            return "UNAUTHENTICATED"
        dashboard_user = self.get_dashboard_username()
        if dashboard_user is None:
            # Cannot prove who the dashboard administrator is => no operator privileges.
            return "DASHBOARD_USER"
        if u == dashboard_user:
            return "OWNER"
        return "DASHBOARD_USER"


def build_dashboard_username_getter(context: Any) -> Callable[[], str | None]:
    """Build a getter for ``dashboard.username`` from the official ``Context.get_config()``."""

    def _get() -> str | None:
        if context is None or not hasattr(context, "get_config"):
            return None
        cfg = context.get_config()
        if not isinstance(cfg, dict):
            return None
        dashboard = cfg.get("dashboard")
        if not isinstance(dashboard, dict):
            return None
        username = dashboard.get("username")
        return str(username).strip() if username else None

    return _get
