"""Data models, enums and type definitions for astrbot_plugin_group_approval."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class ApplicationStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class GrantStatus(str, Enum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"


class Role(str, Enum):
    OWNER = "OWNER"
    REVIEWER = "REVIEWER"
    APPROVED_USER = "APPROVED_USER"
    UNAUTHORIZED = "UNAUTHORIZED"


class RestorePolicy(str, Enum):
    AUTOMATIC = "automatic"
    MANUAL = "manual"


@dataclass
class GroupMemberLevelResult:
    """Result of querying a group member's level from NapCat."""

    success: bool
    level: int = 0
    role: str = "member"  # "owner", "admin", "member"
    is_member: bool = False
    group_id: str = ""
    user_id: str = ""
    checked_at: int = field(default_factory=lambda: int(time.time()))
    error_code: str = ""
    raw_data: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GroupPolicy:
    """Configuration policy for an individual QQ group."""

    bot_self_id: str
    group_id: str
    enabled: bool = True
    min_level: int = 10
    approval_required: bool = True
    grant_duration_seconds: int = 0  # 0 = permanent
    recheck_interval_seconds: int = 300
    restore_policy: str = RestorePolicy.AUTOMATIC.value
    owner_bypass: bool = True
    group_admin_bypass: bool = False
    created_at: int = field(default_factory=lambda: int(time.time()))
    updated_at: int = field(default_factory=lambda: int(time.time()))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Application:
    """Access application submitted by a group member."""

    id: str  # Unique code, e.g. A1024
    bot_self_id: str
    group_id: str
    user_id: str
    level_at_application: int
    status: ApplicationStatus = ApplicationStatus.PENDING
    reason: str = ""
    created_at: int = field(default_factory=lambda: int(time.time()))
    expires_at: int = 0
    reviewed_at: int | None = None
    reviewer_id: str | None = None
    review_note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value if isinstance(self.status, ApplicationStatus) else self.status
        return data


@dataclass
class Grant:
    """Access grant record for an approved user."""

    id: str
    bot_self_id: str
    group_id: str
    user_id: str
    status: GrantStatus = GrantStatus.ACTIVE
    source_application_id: str | None = None
    approved_by: str = ""
    approved_at: int = field(default_factory=lambda: int(time.time()))
    expires_at: int = 0  # 0 = permanent
    last_verified_level: int = 0
    last_verified_at: int = field(default_factory=lambda: int(time.time()))
    suspend_reason: str | None = None
    revoked_by: str | None = None
    revoked_at: int | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value if isinstance(self.status, GrantStatus) else self.status
        return data


@dataclass
class BlacklistEntry:
    """Blacklist entry forbidding a user from access or application."""

    bot_self_id: str
    group_id: str  # specific group or "ALL"
    user_id: str
    reason: str = ""
    created_by: str = ""
    created_at: int = field(default_factory=lambda: int(time.time()))
    expires_at: int = 0  # 0 = permanent

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AuditLogEntry:
    """Audit log item recording administrative or system security events."""

    id: int | None = None
    timestamp: int = field(default_factory=lambda: int(time.time()))
    actor_id: str = ""
    actor_type: str = "SYSTEM"  # OWNER, REVIEWER, SYSTEM, USER
    bot_self_id: str = ""
    group_id: str = ""
    target_user_id: str = ""
    action: str = ""
    old_state: str | None = None
    new_state: str | None = None
    request_id: str | None = None
    details_json: str = "{}"

    def get_details(self) -> dict[str, Any]:
        try:
            return json.loads(self.details_json)
        except Exception:
            return {}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AccessDecision:
    """Result returned by the permission gate."""

    allowed: bool
    reason: str
    grant_status: str | None = None
    current_level: int = 0
    minimum_level: int = 0
    last_verified_at: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class NotificationItem:
    """Queued notification in outbox."""

    id: int | None = None
    event_key: str = ""
    recipient_id: str = ""  # User ID or Group ID
    channel: str = "private"  # "private" or "group"
    payload: str = ""
    status: str = "PENDING"  # PENDING, SENT, FAILED
    attempts: int = 0
    next_retry_at: int = field(default_factory=lambda: int(time.time()))
    created_at: int = field(default_factory=lambda: int(time.time()))
    sent_at: int | None = None
    last_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
