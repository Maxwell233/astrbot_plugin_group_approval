"""Configuration parsing, defaults, and validation for Group Approval."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ApplicationConfig:
    expire_hours: int = 72
    reject_cooldown_hours: int = 24
    duplicate_pending_allowed: bool = False
    rate_limit_per_hour: int = 5


@dataclass
class PrivateChatConfig:
    enabled_for_normal_users: bool = False
    admin_commands_enabled: bool = True


@dataclass
class NotificationConfig:
    reviewer_private_message: bool = True
    fallback_group_id: str = ""
    retry_enabled: bool = True
    max_retries: int = 5
    retry_interval_seconds: int = 60


@dataclass
class SecurityConfig:
    fail_closed: bool = True
    gate_priority: int = 50
    log_denied_messages: bool = False
    keep_audit_days: int = 90
    command_prefix_required_for_rejection_notice: bool = True


@dataclass
class PluginConfig:
    enabled: bool = True
    default_policy: str = "deny"  # "deny" or "allow"
    owner_qq_ids: list[str] = field(default_factory=list)
    reviewer_qq_ids: list[str] = field(default_factory=list)
    groups: dict[str, dict[str, Any]] = field(default_factory=dict)
    application: ApplicationConfig = field(default_factory=ApplicationConfig)
    private_chat: PrivateChatConfig = field(default_factory=PrivateChatConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> PluginConfig:
        if not data:
            return cls()

        owner_ids = [str(x).strip() for x in data.get("owner_qq_ids", []) if str(x).strip()]
        reviewer_ids = [str(x).strip() for x in data.get("reviewer_qq_ids", []) if str(x).strip()]

        # Parse application config
        app_raw = data.get("application", {})
        app_cfg = ApplicationConfig(
            expire_hours=int(app_raw.get("expire_hours", 72)),
            reject_cooldown_hours=int(app_raw.get("reject_cooldown_hours", 24)),
            duplicate_pending_allowed=bool(app_raw.get("duplicate_pending_allowed", False)),
            rate_limit_per_hour=int(app_raw.get("rate_limit_per_hour", 5)),
        )

        # Parse private chat config
        priv_raw = data.get("private_chat", {})
        priv_cfg = PrivateChatConfig(
            enabled_for_normal_users=bool(priv_raw.get("enabled_for_normal_users", False)),
            admin_commands_enabled=bool(priv_raw.get("admin_commands_enabled", True)),
        )

        # Parse notifications
        notif_raw = data.get("notifications", {})
        notif_cfg = NotificationConfig(
            reviewer_private_message=bool(notif_raw.get("reviewer_private_message", True)),
            fallback_group_id=str(notif_raw.get("fallback_group_id", "")).strip(),
            retry_enabled=bool(notif_raw.get("retry_enabled", True)),
            max_retries=int(notif_raw.get("max_retries", 5)),
            retry_interval_seconds=int(notif_raw.get("retry_interval_seconds", 60)),
        )

        # Parse security
        sec_raw = data.get("security", {})
        sec_cfg = SecurityConfig(
            fail_closed=bool(sec_raw.get("fail_closed", True)),
            gate_priority=int(sec_raw.get("gate_priority", 50)),
            log_denied_messages=bool(sec_raw.get("log_denied_messages", False)),
            keep_audit_days=int(sec_raw.get("keep_audit_days", 90)),
            command_prefix_required_for_rejection_notice=bool(
                sec_raw.get("command_prefix_required_for_rejection_notice", True)
            ),
        )

        groups_raw = data.get("groups", {})
        groups_clean: dict[str, dict[str, Any]] = {}
        if isinstance(groups_raw, dict):
            for gid, gcfg in groups_raw.items():
                if isinstance(gcfg, dict):
                    groups_clean[str(gid).strip()] = {
                        "enabled": bool(gcfg.get("enabled", True)),
                        "min_level": int(gcfg.get("min_level", 10)),
                        "approval_required": bool(gcfg.get("approval_required", True)),
                        "grant_duration_days": int(gcfg.get("grant_duration_days", 0)),
                        "recheck_interval_seconds": int(gcfg.get("recheck_interval_seconds", 300)),
                        "restore_policy": str(gcfg.get("restore_policy", "automatic")),
                        "owner_bypass": bool(gcfg.get("owner_bypass", True)),
                        "group_admin_bypass": bool(gcfg.get("group_admin_bypass", False)),
                    }

        return cls(
            enabled=bool(data.get("enabled", True)),
            default_policy=str(data.get("default_policy", "deny")).lower(),
            owner_qq_ids=owner_ids,
            reviewer_qq_ids=reviewer_ids,
            groups=groups_clean,
            application=app_cfg,
            private_chat=priv_cfg,
            notifications=notif_cfg,
            security=sec_cfg,
        )

    def is_owner(self, user_id: str) -> bool:
        return str(user_id).strip() in self.owner_qq_ids

    def is_reviewer(self, user_id: str) -> bool:
        uid = str(user_id).strip()
        return uid in self.reviewer_qq_ids or uid in self.owner_qq_ids
