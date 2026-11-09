"""NapCat / OneBot v11 client adapter for AstrBot.

Provides response normalization, multi-bot routing, and error classification.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

logger = logging.getLogger("astrbot_plugin_group_approval.napcat")


def normalize_onebot_response(resp: Any) -> tuple[bool, dict[str, Any] | None, str, int]:
    """Normalize response from raw OneBot v11 or AstrBot platform adapter.

    Returns:
        (success: bool, data: dict | None, error_msg: str, error_code: int)
    """
    if resp is None:
        return False, None, "Empty response from platform adapter", -4

    # Direct integer or string message_id (some adapters return message_id directly)
    if isinstance(resp, (int, str)):
        return True, {"message_id": resp}, "", 0

    if isinstance(resp, dict):
        # 1. Direct message send result (has message_id)
        if "message_id" in resp:
            return True, resp, "", 0

        # 2. Standard OneBot v11 wrapped format with 'retcode' or 'status'
        if "retcode" in resp or "status" in resp:
            has_status = "status" in resp
            has_retcode = "retcode" in resp
            status_val = str(resp.get("status", "")).strip().lower() if has_status else None
            retcode_val = resp.get("retcode") if has_retcode else None

            is_ok = True
            if has_status and status_val != "ok":
                is_ok = False
            if has_retcode:
                try:
                    if int(retcode_val) != 0:
                        is_ok = False
                except (ValueError, TypeError):
                    is_ok = False

            if is_ok and (has_status or has_retcode):
                data = resp.get("data")
                if data is None:
                    # In some OneBot actions, payload is at root
                    clean = {k: v for k, v in resp.items() if k not in ("status", "retcode", "echo")}
                    return True, clean, "", 0
                if isinstance(data, dict):
                    return True, data, "", 0
                return True, {"value": data}, "", 0
            else:
                msg = resp.get("msg") or resp.get("message") or f"OneBot API error (retcode={retcode_val})"
                try:
                    code = int(retcode_val) if retcode_val is not None else -4
                except (ValueError, TypeError):
                    code = -4
                return False, None, str(msg), code

        # 3. Direct unpacked dictionary returned by some AstrBot adapters
        # Check for member info dictionary (has user_id, group_id, or level)
        if "user_id" in resp or "group_id" in resp or "level" in resp:
            return True, resp, "", 0

        # Empty dictionary in some adapters indicates success with no payload
        if not resp:
            return True, {}, "", 0

        # Fallback dictionary
        return True, resp, "", 0

    return False, None, f"Unsupported response type: {type(resp)}", -4


class NapCatClient:
    """Adapter for executing OneBot v11 protocol actions via AstrBot platform client."""

    def __init__(
        self,
        call_action_fn: Callable[..., Coroutine[Any, Any, Any]] | None = None,
        timeout: float = 5.0,
    ):
        self._custom_call_action = call_action_fn
        self.timeout = timeout
        self._context: Any = None

    def set_context(self, context: Any) -> None:
        self._context = context

    async def call_action(
        self,
        action: str,
        bot_self_id: str | None = None,
        **params: Any,
    ) -> tuple[bool, dict[str, Any] | None, str, int]:
        """Invoke an action with timeout protection, multi-bot routing, and normalized response.

        Returns:
            (success: bool, data: dict | None, error_msg: str, error_code: int)
        """
        if self._custom_call_action is not None:
            try:
                raw = await asyncio.wait_for(
                    self._custom_call_action(action, bot_self_id=bot_self_id, **params),
                    timeout=self.timeout,
                )
                return normalize_onebot_response(raw)
            except TimeoutError:
                logger.warning(f"NapCat action '{action}' timed out after {self.timeout}s")
                return False, None, f"Timeout after {self.timeout}s", -1
            except Exception as e:
                logger.error(f"NapCat action '{action}' raised exception: {e}")
                return False, None, str(e), -2

        # Resolve target bot client from AstrBot context
        client = self._resolve_bot_client(bot_self_id)
        if client is None:
            err = f"No active aiocqhttp / OneBot client available for bot_id={bot_self_id or 'any'}"
            logger.warning(err)
            return False, None, err, -3

        try:
            call_fn = getattr(client, "api", client).call_action
            raw = await asyncio.wait_for(call_fn(action, **params), timeout=self.timeout)
            return normalize_onebot_response(raw)
        except TimeoutError:
            logger.warning(f"NapCat action '{action}' timed out after {self.timeout}s")
            return False, None, f"Timeout after {self.timeout}s", -1
        except Exception as e:
            logger.error(f"NapCat action '{action}' call failed: {e}")
            return False, None, str(e), -2

    def _resolve_bot_client(self, bot_self_id: str | None = None) -> Any:
        if self._context is None:
            return None

        target_bid = str(bot_self_id).strip() if bot_self_id else None

        # Check platform_manager
        pm = getattr(self._context, "platform_manager", None)
        if pm is not None:
            get_insts = getattr(pm, "get_insts", None)
            if callable(get_insts):
                insts = get_insts()
                for inst in insts:
                    # Resolve platform name via official meta() or adapter_type
                    p_name = ""
                    meta_fn = getattr(inst, "meta", None)
                    if callable(meta_fn):
                        try:
                            meta_obj = meta_fn()
                            name_val = getattr(meta_obj, "name", "")
                            if isinstance(name_val, str) and name_val:
                                p_name = name_val.lower()
                        except Exception:
                            pass
                    if not p_name:
                        for attr in ("adapter_type", "platform_name"):
                            val = getattr(inst, attr, None)
                            if isinstance(val, str) and val:
                                p_name = val.lower()
                                break

                    if "aiocqhttp" in p_name or "onebot" in p_name:
                        client = getattr(inst, "get_client", lambda: None)()
                        if client is None:
                            client = getattr(inst, "bot", None)

                        if client is not None:
                            c_self_id = str(
                                getattr(client, "self_id", None)
                                or getattr(inst, "self_id", None)
                                or ""
                            ).strip()

                            if not target_bid:
                                return client
                            if c_self_id == target_bid:
                                return client

        # If a specific target bot was requested and not found, strictly do NOT fallback
        if target_bid:
            return None

        # Fallback to get_platform only if no specific bot was requested
        get_platform = getattr(self._context, "get_platform", None)
        if callable(get_platform):
            try:
                p = get_platform("aiocqhttp")
                if p is not None:
                    return getattr(p, "get_client", lambda: p)()
            except Exception:
                pass

        return None

    async def get_group_member_info(
        self,
        group_id: str,
        user_id: str,
        bot_self_id: str | None = None,
        no_cache: bool = True,
    ) -> tuple[bool, dict[str, Any] | None, str, int]:
        """Fetch group member info including level and role."""
        payload = {
            "group_id": int(group_id),
            "user_id": int(user_id),
            "no_cache": bool(no_cache),
        }
        return await self.call_action("get_group_member_info", bot_self_id=bot_self_id, **payload)

    async def send_private_msg(
        self,
        user_id: str,
        message: str,
        bot_self_id: str | None = None,
    ) -> tuple[bool, dict[str, Any] | None, str, int]:
        """Send private message to QQ user."""
        payload = {
            "user_id": int(user_id),
            "message": message,
        }
        return await self.call_action("send_private_msg", bot_self_id=bot_self_id, **payload)

    async def send_group_msg(
        self,
        group_id: str,
        message: str,
        bot_self_id: str | None = None,
    ) -> tuple[bool, dict[str, Any] | None, str, int]:
        """Send message to QQ group."""
        payload = {
            "group_id": int(group_id),
            "message": message,
        }
        return await self.call_action("send_group_msg", bot_self_id=bot_self_id, **payload)
