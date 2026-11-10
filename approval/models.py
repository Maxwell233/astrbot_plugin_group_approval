"""Shared model types for the member lookup and audit log."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any


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
