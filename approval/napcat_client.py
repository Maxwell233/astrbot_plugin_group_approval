"""NapCat / OneBot v11 client adapter for AstrBot."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

logger = logging.getLogger("astrbot_plugin_group_approval.napcat")


class NapCatClient:
    """Adapter for executing OneBot v11 protocol actions via AstrBot platform client."""

    def __init__(
        self,
        call_action_fn: Callable[..., Coroutine[Any, Any, dict[str, Any]]] | None = None,
        timeout: float = 5.0,
    ):
        self._custom_call_action = call_action_fn
        self.timeout = timeout
        self._context: Any = None

    def set_context(self, context: Any) -> None:
        self._context = context

    async def call_action(self, action: str, **params: Any) -> dict[str, Any]:
        """Invoke an action with timeout protection and structured error handling."""
        if self._custom_call_action is not None:
            try:
                return await asyncio.wait_for(
                    self._custom_call_action(action, **params),
                    timeout=self.timeout,
                )
            except TimeoutError:
                logger.warning(f"NapCat action '{action}' timed out after {self.timeout}s")
                return {"status": "failed", "retcode": -1, "msg": f"Timeout after {self.timeout}s"}
            except Exception as e:
                logger.error(f"NapCat action '{action}' raised exception: {e}")
                return {"status": "failed", "retcode": -2, "msg": str(e)}

        # Attempt to resolve bot client from AstrBot context
        client = self._resolve_bot_client()
        if client is None:
            logger.warning(f"No active aiocqhttp / OneBot client available to invoke '{action}'")
            return {"status": "failed", "retcode": -3, "msg": "No active platform client"}

        try:
            call_fn = getattr(client, "api", client).call_action
            return await asyncio.wait_for(call_fn(action, **params), timeout=self.timeout)
        except TimeoutError:
            logger.warning(f"NapCat action '{action}' timed out after {self.timeout}s")
            return {"status": "failed", "retcode": -1, "msg": f"Timeout after {self.timeout}s"}
        except Exception as e:
            logger.error(f"NapCat action '{action}' call failed: {e}")
            return {"status": "failed", "retcode": -4, "msg": str(e)}

    def _resolve_bot_client(self) -> Any:
        if self._context is None:
            return None

        # Check platform_manager
        pm = getattr(self._context, "platform_manager", None)
        if pm is not None:
            get_insts = getattr(pm, "get_insts", None)
            if callable(get_insts):
                insts = get_insts()
                for inst in insts:
                    adapter_type = getattr(inst, "adapter_type", "") or getattr(inst, "platform_name", "")
                    if "aiocqhttp" in str(adapter_type).lower():
                        client = getattr(inst, "get_client", lambda: None)()
                        if client is not None:
                            return client
                        bot = getattr(inst, "bot", None)
                        if bot is not None:
                            return bot

        # Try context.get_platform("aiocqhttp")
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
        no_cache: bool = True,
    ) -> dict[str, Any]:
        """Fetch group member info including level and role."""
        payload = {
            "group_id": int(group_id),
            "user_id": int(user_id),
            "no_cache": bool(no_cache),
        }
        return await self.call_action("get_group_member_info", **payload)

    async def send_private_msg(self, user_id: str, message: str) -> dict[str, Any]:
        """Send private message to QQ user."""
        payload = {
            "user_id": int(user_id),
            "message": message,
        }
        return await self.call_action("send_private_msg", **payload)

    async def send_group_msg(self, group_id: str, message: str) -> dict[str, Any]:
        """Send message to QQ group."""
        payload = {
            "group_id": int(group_id),
            "message": message,
        }
        return await self.call_action("send_group_msg", **payload)
