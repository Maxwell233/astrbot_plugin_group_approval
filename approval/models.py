"""Shared model types for the member lookup."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
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
