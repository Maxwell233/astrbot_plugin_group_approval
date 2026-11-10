"""Group member level lookup service with request coalescing."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from .models import GroupMemberLevelResult
from .napcat_client import (
    BOT_IDENTITY_ERROR_CODES,
    NapCatClient,
)

logger = logging.getLogger("astrbot_plugin_group_approval.level")

# Regex to strictly parse level strings (e.g., "LV18", "lv 18", "等级18", "18")
STRICT_LEVEL_REGEX = re.compile(r"^(?:LV|lv|Lv|等级)?\s*(\d+)$")


class LevelService:
    """Fetch fresh NapCat levels and share equivalent in-flight lookups."""

    def __init__(self, napcat_client: NapCatClient):
        self.client = napcat_client
        self._in_flight: dict[
            tuple[str, str, str, tuple[int, int] | None],
            asyncio.Future[GroupMemberLevelResult],
        ] = {}

    def parse_level(self, raw_level: Any) -> int | None:
        """Strictly parse member level.

        Returns integer if valid, or None if invalid/missing.
        Rejects arbitrary messy strings like 'rank_999_user'.
        """
        if raw_level is None or isinstance(raw_level, bool):
            return None

        if isinstance(raw_level, int):
            return raw_level if raw_level >= 0 else None

        raw_str = str(raw_level).strip()
        if not raw_str:
            return None

        # Check for numeric or LV-prefixed format strictly
        match = STRICT_LEVEL_REGEX.match(raw_str)
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                return None
        return None

    async def get_group_member_level(
        self,
        bot_id: str,
        group_id: str,
        user_id: str,
        *,
        lookup_generation: tuple[int, int] | None = None,
    ) -> GroupMemberLevelResult:
        """Share only queries for the same Bot, member and snapshot generation."""
        request_key = (bot_id, group_id, user_id, lookup_generation)
        pending = self._in_flight.get(request_key)
        if pending is not None:
            return await asyncio.shield(pending)

        pending = asyncio.get_running_loop().create_future()
        self._in_flight[request_key] = pending
        try:
            result = await self._fetch_from_napcat(bot_id, group_id, user_id)
        except asyncio.CancelledError:
            pending.set_result(GroupMemberLevelResult(
                success=False, group_id=group_id, user_id=user_id, error_code="QUERY_CANCELLED",
            ))
            raise
        except Exception as exc:
            logger.exception("NapCat level lookup raised an unexpected exception")
            result = GroupMemberLevelResult(
                success=False, group_id=group_id, user_id=user_id,
                error_code=f"QUERY_EXCEPTION_{type(exc).__name__}",
            )
        finally:
            del self._in_flight[request_key]

        pending.set_result(result)
        return result

    async def _fetch_from_napcat(self, bot_id: str, group_id: str, user_id: str) -> GroupMemberLevelResult:
        ok, data, err_msg, retcode = await self.client.get_group_member_info(
            group_id=group_id, user_id=user_id, bot_self_id=bot_id, no_cache=True,
        )

        # Check for successful OneBot response
        if not ok or not isinstance(data, dict):
            logger.warning(
                f"NapCat get_group_member_info failed for bot={bot_id}, group={group_id}, user={user_id}: {err_msg} (retcode={retcode})"
            )
            # Fail closed on bot-identity resolution: surface a stable error code
            # instead of a generic retcode so callers can distinguish "bot not
            # connected/verified" from protocol-level member lookup failures.
            error_text = str(err_msg or "")
            identity_error = next(
                (code for code in BOT_IDENTITY_ERROR_CODES if error_text.startswith(code)),
                None,
            )
            if identity_error is not None:
                logger.error(
                    f"Refusing level lookup for unverified bot {bot_id!r}: {error_text[:200]}"
                )
                error_code = identity_error
            else:
                error_code = f"RETCODE_{retcode}" if retcode != 0 else error_text
            return GroupMemberLevelResult(
                success=False,
                level=0,
                role="",
                is_member=False,
                group_id=group_id,
                user_id=user_id,
                error_code=error_code,
                raw_data={"error_msg": err_msg, "retcode": retcode},
            )

        # Validate that returned group_id and user_id match
        resp_gid = str(data.get("group_id", "")).strip()
        resp_uid = str(data.get("user_id", "")).strip()
        if resp_gid != group_id:
            logger.error(f"Identity mismatch: expected group {group_id}, got {resp_gid}")
            return GroupMemberLevelResult(
                success=False,
                level=0,
                is_member=False,
                group_id=group_id,
                user_id=user_id,
                error_code="GROUP_ID_MISMATCH",
                raw_data=data,
            )

        if resp_uid != user_id:
            logger.error(f"Identity mismatch: expected user {user_id}, got {resp_uid}")
            return GroupMemberLevelResult(
                success=False,
                level=0,
                is_member=False,
                group_id=group_id,
                user_id=user_id,
                error_code="USER_ID_MISMATCH",
                raw_data=data,
            )

        if data.get("is_member") is False:
            return GroupMemberLevelResult(
                success=False,
                level=0,
                is_member=False,
                group_id=group_id,
                user_id=user_id,
                error_code="NOT_MEMBER",
            )

        # Extract group level (CRITICAL: 'level', NEVER 'qq_level')
        # In NapCat, level corresponds to memberRealLevel
        raw_level = data.get("level")
        parsed_level = self.parse_level(raw_level)

        if parsed_level is None:
            logger.warning(
                f"Invalid or missing group level for group={group_id}, user={user_id}: raw={raw_level}"
            )
            return GroupMemberLevelResult(
                success=False,
                level=0,
                role=str(data.get("role", "member")),
                is_member=True,
                group_id=group_id,
                user_id=user_id,
                error_code="INVALID_LEVEL",
                raw_data=data,
            )

        role = str(data.get("role", "member")).lower()
        if role not in ("owner", "admin", "member"):
            role = "member"

        return GroupMemberLevelResult(
            success=True,
            level=parsed_level,
            role=role,
            is_member=True,
            group_id=group_id,
            user_id=user_id,
            raw_data=data,
        )
