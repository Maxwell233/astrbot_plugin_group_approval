"""Configuration for first-time 0.1.2 installations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ApplicationConfig:
    rate_limit_per_hour: int = 5
    query_limit: int = 3
    query_window_seconds: int = 60


@dataclass
class PrivateChatConfig:
    admin_commands_enabled: bool = True


@dataclass
class NotificationConfig:
    retry_enabled: bool = True
    max_retries: int = 5
    retry_interval_seconds: int = 60


@dataclass
class SecurityConfig:
    keep_audit_days: int = 90


@dataclass
class PluginConfig:
    enabled: bool = True
    owner_qq_ids: list[str] = field(default_factory=list)
    reviewer_qq_ids: list[str] = field(default_factory=list)
    application: ApplicationConfig = field(default_factory=ApplicationConfig)
    private_chat: PrivateChatConfig = field(default_factory=PrivateChatConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> PluginConfig:
        data = data or {}
        application = data.get("application") or {}
        private = data.get("private_chat") or {}
        notifications = data.get("notifications") or {}
        security = data.get("security") or {}
        return cls(
            enabled=bool(data.get("enabled", True)),
            owner_qq_ids=[str(x).strip() for x in data.get("owner_qq_ids", []) if str(x).strip()],
            reviewer_qq_ids=[str(x).strip() for x in data.get("reviewer_qq_ids", []) if str(x).strip()],
            application=ApplicationConfig(
                rate_limit_per_hour=max(0, int(application.get("rate_limit_per_hour", 5))),
                query_limit=max(1, int(application.get("query_limit", 3))),
                query_window_seconds=max(1, int(application.get("query_window_seconds", 60))),
            ),
            private_chat=PrivateChatConfig(admin_commands_enabled=bool(private.get("admin_commands_enabled", True))),
            notifications=NotificationConfig(
                retry_enabled=bool(notifications.get("retry_enabled", True)),
                max_retries=max(1, int(notifications.get("max_retries", 5))),
                retry_interval_seconds=max(1, int(notifications.get("retry_interval_seconds", 60))),
            ),
            security=SecurityConfig(keep_audit_days=max(0, int(security.get("keep_audit_days", 90)))),
        )

    def is_owner(self, user: str) -> bool:
        return str(user).strip() in self.owner_qq_ids
