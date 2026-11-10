"""Group member level lookup service with TTL caching and request coalescing."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from .models import GroupMemberLevelResult
from .napcat_client import (
    BOT_IDENTITY_ERROR_CODES,
    NapCatClient,
    normalize_onebot_response,
)

logger = logging.getLogger("astrbot_plugin_group_approval.level")

# Regex to strictly parse level strings (e.g., "LV18", "lv 18", "等级18", "18")
STRICT_LEVEL_REGEX = re.compile(r"^(?:LV|lv|Lv|等级)?\s*(\d+)$")


class LevelService:
    """Manages group member level lookups, caching, and concurrent request coalescing."""

    def __init__(self, napcat_client: NapCatClient, cache_ttl: float = 300.0):
        self.client = napcat_client
        self.cache_ttl = cache_ttl

        # Cache key: (bot_id, group_id, user_id) -> (GroupMemberLevelResult, expire_timestamp)
        self._cache: dict[tuple[str, str, str], tuple[GroupMemberLevelResult, float]] = {}

        # Request coalescing (single-flight): key -> Future[GroupMemberLevelResult]
        self._in_flight: dict[tuple[str, str, str, bool, tuple[int, int] | None], asyncio.Future[GroupMemberLevelResult]] = {}
        self._lock = asyncio.Lock()

    def parse_level(self, raw_level: Any) -> int | None:
        """Strictly parse member level.

        Returns integer if valid, or None if invalid/missing.
        Rejects arbitrary messy strings like 'rank_999_user'.
        """
        if raw_level is None:
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
        force_refresh: bool = False,
        *,
        lookup_generation: tuple[int, int] | None = None,
    ) -> GroupMemberLevelResult:
        """Query member group level with caching and single-flight concurrency."""
        b_id = str(bot_id).strip()
        g_id = str(group_id).strip()
        u_id = str(user_id).strip()
        key = (b_id, g_id, u_id)
        request_key = (*key, force_refresh, lookup_generation)
        now = time.time()

        # 1. Check cache if not forcing refresh
        if not force_refresh:
            cached = self._cache.get(key)
            if cached is not None:
                res, expire_at = cached
                if now < expire_at:
                    return res

        # 2. Check in-flight requests (request coalescing)
        async with self._lock:
            # Re-check cache under lock
            if not force_refresh:
                cached = self._cache.get(key)
                if cached is not None:
                    res, expire_at = cached
                    if now < expire_at:
                        return res

            # A forced lookup must not join a request that allowed NapCat caching.
            future = self._in_flight.get(request_key)
            if future is not None:
                # Another task is currently fetching the exact same user info
                need_wait = True
            else:
                loop = asyncio.get_running_loop()
                future = loop.create_future()
                self._in_flight[request_key] = future
                need_wait = False

        if need_wait:
            return await asyncio.shield(future)

        # 3. Perform actual remote query
        try:
            result = await self._fetch_from_napcat(b_id, g_id, u_id, force_refresh)
            # Store in cache only on successful member lookup
            expires_at = now + self.cache_ttl
            cached = self._cache.get(key)
            if (lookup_generation is None and result.success and result.is_member
                    and (cached is None or cached[1] <= expires_at)):
                self._cache[key] = (result, expires_at)

            if not future.done():
                future.set_result(result)
            return result
        except asyncio.CancelledError:
            # Followers deny this request instead of waiting forever. The
            # leader still propagates the framework's cancellation.
            if not future.done():
                future.set_result(GroupMemberLevelResult(
                    success=False, group_id=g_id, user_id=u_id, error_code="QUERY_CANCELLED",
                ))
            raise
        except Exception as e:
            err_res = GroupMemberLevelResult(
                success=False,
                level=0,
                is_member=False,
                group_id=g_id,
                user_id=u_id,
                error_code=f"QUERY_EXCEPTION_{type(e).__name__}",
            )
            if not future.done():
                future.set_result(err_res)
            return err_res
        finally:
            async with self._lock:
                self._in_flight.pop(request_key, None)

    async def _fetch_from_napcat(
        self,
        bot_id: str,
        group_id: str,
        user_id: str,
        force_refresh: bool,
    ) -> GroupMemberLevelResult:
        """Call NapCat OneBot API and validate responses."""
        try:
            res = await self.client.get_group_member_info(
                group_id=group_id,
                user_id=user_id,
                bot_self_id=bot_id,
                no_cache=force_refresh,
            )
        except TypeError:
            res = await self.client.get_group_member_info(
                group_id=group_id,
                user_id=user_id,
                no_cache=force_refresh,
            )

        if isinstance(res, tuple) and len(res) == 4:
            ok, data, err_msg, retcode = res
        else:
            ok, data, err_msg, retcode = normalize_onebot_response(res)

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

    def invalidate_cache(self, bot_id: str, group_id: str, user_id: str) -> None:
        """Evict cache entry."""
        key = (str(bot_id).strip(), str(group_id).strip(), str(user_id).strip())
        self._cache.pop(key, None)

    def clear_cache(self) -> None:
        """Clear all cached level entries."""
        self._cache.clear()
