"""NapCat / OneBot v11 client adapter for AstrBot.

Multi-bot routing uses AstrBot's platform API:

- ``context.platform_manager.get_insts()`` (or ``platform_insts``)
- ``inst.meta().name == "aiocqhttp"`` and ``inst.get_client() -> CQHttp``
- ``await client.call_action("get_login_info")`` verifies the real bot QQ

Aiocqhttp routes a OneBot API call by the ``self_id`` parameter of
``call_action`` (``WebSocketReverseApi.call_action`` selects the reverse-WS
client keyed by the OneBot ``self_id``/``X-Self-ID``).  Therefore every
targeted action here forwards ``self_id=<verified QQ>`` explicitly.

Identity policy (fail closed):

- Reverse-WebSocket accounts must match a routed ``get_login_info`` probe.
- Dashboard writes require a fresh login probe.
- Unknown ids never fall back to "the first instance".
- Untargeted calls are only allowed when exactly one aiocqhttp bot is verified.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger

try:
    from aiocqhttp.exceptions import ActionFailed
except ImportError:  # Standalone service tests do not install AstrBot's OneBot SDK.
    ACTION_FAILED_ERRORS = ()
else:
    ACTION_FAILED_ERRORS = (ActionFailed,)

AIOCQHTTP_PLATFORM_NAME = "aiocqhttp"

# Fail-closed error codes surfaced in the (ok, data, err_msg, retcode) tuple.
ERR_UNKNOWN_BOT_SELF_ID = "UNKNOWN_BOT_SELF_ID"
ERR_NO_VERIFIED_BOT = "NO_VERIFIED_BOT"
ERR_AMBIGUOUS_BOT = "AMBIGUOUS_BOT_SELF_ID"
ERR_INVALID_BOT_CLIENT = "INVALID_BOT_CLIENT"

BOT_IDENTITY_ERROR_CODES = (
    ERR_UNKNOWN_BOT_SELF_ID,
    ERR_NO_VERIFIED_BOT,
    ERR_AMBIGUOUS_BOT,
    ERR_INVALID_BOT_CLIENT,
)

# How long a verified binding may be reused before it is re-verified.
IDENTITY_TTL_SECONDS = 5.0


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

        return True, resp, "", 0

    return False, None, f"Unsupported response type: {type(resp)}", -4


def _normalize_qq(value: Any) -> str | None:
    """Return a canonical numeric QQ string, or None when the value is unusable."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not (text.isascii() and text.isdecimal() and len(text) <= 20 and text[0] != "0"):
        return None
    return text


def _safe_call(obj: Any, method_name: str) -> Any:
    """Call ``obj.method_name()`` defensively (attribute may be absent/raise)."""
    method = getattr(obj, method_name, None)
    if not callable(method):
        return None
    try:
        return method()
    except Exception as exc:  # noqa: BLE001 - platform objects are third-party code
        logger.debug("Failed to call %s.%s(): %s", type(obj).__name__, method_name, exc)
        return None


def _platform_meta_name(inst: Any) -> str | None:
    meta = _safe_call(inst, "meta")
    if meta is None:
        return None
    name = getattr(meta, "name", None)
    return name.strip() if isinstance(name, str) and name.strip() else None


def _platform_meta_id(inst: Any) -> str | None:
    meta = _safe_call(inst, "meta")
    if meta is not None:
        meta_id = getattr(meta, "id", None)
        if isinstance(meta_id, str) and meta_id.strip():
            return meta_id.strip()
    config = getattr(inst, "config", None)
    if isinstance(config, dict):
        config_id = config.get("id")
        if isinstance(config_id, str) and config_id.strip():
            return config_id.strip()
    return None


def _resolve_call_action(client: Any) -> Callable[..., Coroutine[Any, Any, Any]] | None:
    """Return a callable OneBot transport for ``client``.

    AstrBot's aiocqhttp adapter exposes ``CQHttp.call_action``; the raw
    aiocqhttp SDK additionally exposes ``CQHttp.api.call_action``.
    """
    direct = getattr(client, "call_action", None)
    if callable(direct):
        return direct
    api = getattr(client, "api", None)
    via_api = getattr(api, "call_action", None)
    if callable(via_api):
        return via_api
    return None


@dataclass
class _BotIdentity:
    """A verified bot account bound to a platform instance."""

    self_id: str
    platform_id: str
    instance: Any
    client: Any
    verified_at: float
    socket: Any = None


class NapCatClient:
    """Adapter for executing OneBot v11 protocol actions via AstrBot platform client."""

    def __init__(self, *, timeout: float = 5.0):
        self.timeout = timeout
        self._context: Any = None
        self._bindings: dict[tuple[str, str], _BotIdentity] = {}
        self._lock = asyncio.Lock()

    def set_context(self, context: Any) -> None:
        if context is not self._context:
            self._context = context
            self.invalidate_bot_cache()

    # ------------------------------------------------------------------ cache

    def invalidate_bot_cache(
        self,
        bot_self_id: str | None = None,
        platform_id: str | None = None,
    ) -> None:
        """Drop cached bot identity bindings.

        - ``platform_id`` set: forget that platform instance.
        - ``bot_self_id`` set: forget every platform instance verified as that QQ.
        - neither: forget everything.

        Intended for reconnect handling and tests; normal call paths invalidate
        the affected binding automatically when the transport fails.
        """
        for key, binding in list(self._bindings.items()):
            if platform_id is not None and binding.platform_id != str(platform_id).strip():
                continue
            if bot_self_id is not None and binding.self_id != str(bot_self_id).strip():
                continue
            self._bindings.pop(key, None)

    # -------------------------------------------------------------- discovery

    def _iter_platform_instances(self) -> list[Any]:
        if self._context is None:
            return []
        platform_manager = getattr(self._context, "platform_manager", None)
        if platform_manager is None:
            return []
        get_insts = getattr(platform_manager, "get_insts", None)
        instances: Any = None
        if callable(get_insts):
            try:
                instances = get_insts()
            except Exception as exc:  # noqa: BLE001 - third-party manager
                logger.debug("platform_manager.get_insts() failed: %s", exc)
                return []
        else:
            instances = getattr(platform_manager, "platform_insts", None)
        if instances is None:
            return []
        try:
            candidates = list(instances)
        except TypeError:
            return []
        return [inst for inst in candidates if _platform_meta_name(inst) == AIOCQHTTP_PLATFORM_NAME]

    def _platform_id(self, inst: Any, index: int) -> str:
        platform_id = _platform_meta_id(inst)
        if platform_id:
            return platform_id
        return f"instance-{index}-{id(inst)}"

    def _get_instance_client(self, inst: Any) -> Any:
        get_client = getattr(inst, "get_client", None)
        if callable(get_client):
            try:
                client = get_client()
            except Exception as exc:  # noqa: BLE001 - third-party adapter
                logger.debug("platform instance get_client() failed: %s", exc)
                client = None
            if client is not None and _resolve_call_action(client) is not None:
                return client
        bot = getattr(inst, "bot", None)
        if bot is not None and _resolve_call_action(bot) is not None:
            return bot
        return None

    def _find_instance(self, platform_id: str) -> Any | None:
        for index, inst in enumerate(self._iter_platform_instances()):
            if self._platform_id(inst, index) == platform_id:
                return inst
        return None

    async def _query_login_info(
        self,
        client: Any,
        *,
        self_id_hint: str | None = None,
    ) -> str | None:
        """Call ``get_login_info`` through the official client and return the bot QQ."""
        call_action = _resolve_call_action(client)
        if call_action is None:
            return None
        kwargs: dict[str, Any] = {}
        if self_id_hint:
            # Route the query when the reverse-WS client hosts several bots.
            kwargs["self_id"] = self_id_hint
        try:
            raw = await asyncio.wait_for(
                call_action("get_login_info", **kwargs),
                timeout=self.timeout,
            )
        except TimeoutError:
            logger.warning("get_login_info timed out after %ss", self.timeout)
            return None
        except Exception as exc:  # noqa: BLE001 - transport/connection error
            logger.warning("get_login_info failed: %s", exc)
            return None

        ok, data, error_message, retcode = normalize_onebot_response(raw)
        if not ok or not isinstance(data, dict):
            logger.warning("get_login_info rejected: %s (retcode=%s)", error_message, retcode)
            return None
        for key in ("user_id", "self_id"):
            qq = _normalize_qq(data.get(key))
            if qq is not None:
                return qq
        logger.warning("get_login_info response has no usable user_id")
        return None

    @staticmethod
    def _reverse_sockets(client: Any) -> dict | None:
        # aiocqhttp has no public connection enumerator. The registry provides
        # candidate routing keys only; a matching login response verifies them.
        sockets = getattr(client, "_wsr_api_clients", None)
        return sockets if isinstance(sockets, dict) else None

    async def _discover(self) -> None:
        """Verify each live account independently and evict replaced connections."""
        if self._context is None:
            return

        async with self._lock:
            current_instances = {}
            for index, inst in enumerate(self._iter_platform_instances()):
                platform_id = self._platform_id(inst, index)
                client = self._get_instance_client(inst)
                if client is None:
                    continue
                current_instances[platform_id] = (inst, client)
                sockets = self._reverse_sockets(client)
                known = {
                    binding.self_id: binding
                    for binding in self._bindings.values()
                    if binding.platform_id == platform_id
                    and binding.instance is inst
                    and binding.client is client
                }
                if sockets is not None:
                    candidates = {
                        key: socket for key, socket in sockets.items() if _normalize_qq(key) == key
                    }
                else:
                    candidates = dict.fromkeys(known) or {None: None}

                for hint, socket in candidates.items():
                    previous = known.get(hint)
                    now = time.monotonic()
                    if (
                        previous is not None
                        and previous.socket is socket
                        and now - previous.verified_at < IDENTITY_TTL_SECONDS
                    ):
                        continue

                    qq = await self._query_login_info(client, self_id_hint=hint)
                    if qq is not None and (hint is None or hint == qq):
                        if sockets is not None and sockets.get(qq) is not socket:
                            continue
                        self._bindings[platform_id, qq] = _BotIdentity(
                            qq, platform_id, inst, client, time.monotonic(), socket
                        )
                    elif hint is not None:
                        self._bindings.pop((platform_id, hint), None)
                        if sockets is None:
                            # A registry-less single-account transport can reconnect as a new QQ.
                            replacement = await self._query_login_info(client)
                            if replacement is not None:
                                self._bindings[platform_id, replacement] = _BotIdentity(
                                    replacement, platform_id, inst, client, time.monotonic()
                                )

            for key, binding in list(self._bindings.items()):
                sockets = self._reverse_sockets(binding.client)
                instance = current_instances.get(binding.platform_id)
                if instance is not None and instance[0] is binding.instance and instance[1] is binding.client:
                    if sockets is None or sockets.get(binding.self_id) is binding.socket:
                        continue
                self._bindings.pop(key, None)

    def _match_binding(
        self,
        bot_self_id: str,
        platform_id: str | None,
    ) -> _BotIdentity | None:
        bindings = [
            binding
            for binding in self._bindings.values()
            if platform_id is None or binding.platform_id == platform_id
        ]
        if bot_self_id:
            matches = [binding for binding in bindings if binding.self_id == bot_self_id]
            if not matches:
                return None
            if len(matches) == 1:
                return matches[0]
            # Same account connected through several platform ids: deterministic,
            # most recently verified connection, without collapsing the bindings.
            matches.sort(key=lambda binding: (binding.verified_at, binding.platform_id))
            logger.info(
                "Bot %s is bound to %s platform instances; using %s",
                bot_self_id,
                len(matches),
                matches[-1].platform_id,
            )
            return matches[-1]
        if len(bindings) == 1:
            return bindings[0]
        return None

    async def _resolve_verified(
        self,
        bot_self_id: str | None,
        platform_id: str | None,
    ) -> _BotIdentity | None:
        target = str(bot_self_id).strip() if bot_self_id is not None else ""
        normalized_platform_id = str(platform_id).strip() if platform_id is not None else None
        if normalized_platform_id == "":
            normalized_platform_id = None

        await self._discover()
        return self._match_binding(target, normalized_platform_id)

    async def has_verified_bot(self, bot_self_id: str, platform_id: str) -> bool:
        """Confirm an inbound Bot is bound to this AIOCQHTTP instance."""
        if not platform_id:
            return False
        return await self._resolve_verified(bot_self_id, platform_id) is not None

    async def verify_bot(self, bot_self_id: str, *, platform_id: str | None = None) -> bool:
        """Require a fresh routed login probe for an administrative mutation."""
        binding = await self._resolve_verified(bot_self_id, platform_id)
        if binding is None:
            return False
        qq = await self._query_login_info(binding.client, self_id_hint=binding.self_id)
        sockets = self._reverse_sockets(binding.client)
        inst = self._find_instance(binding.platform_id)
        if (
            qq == bot_self_id
            and inst is not None
            and inst is binding.instance
            and self._get_instance_client(inst) is binding.client
            and (sockets is None or sockets.get(qq) is binding.socket)
        ):
            return True
        self.invalidate_bot_cache(bot_self_id, binding.platform_id)
        return False

    def _fail_closed(
        self,
        bot_self_id: str | None,
        platform_id: str | None,
    ) -> tuple[bool, dict[str, Any] | None, str, int]:
        target = str(bot_self_id).strip() if bot_self_id is not None else ""
        normalized_platform_id = str(platform_id).strip() if platform_id is not None else None
        bindings = [
            binding
            for binding in self._bindings.values()
            if not normalized_platform_id or binding.platform_id == normalized_platform_id
        ]
        if target:
            message = (
                f"{ERR_UNKNOWN_BOT_SELF_ID}: no verified aiocqhttp bot with "
                f"self_id={target!r} (verified bots: {sorted(b.self_id for b in bindings)})"
            )
        elif not bindings:
            scope = f" for platform {normalized_platform_id!r}" if normalized_platform_id else ""
            message = f"{ERR_NO_VERIFIED_BOT}: no aiocqhttp bot verified{scope}"
        else:
            message = (
                f"{ERR_AMBIGUOUS_BOT}: {len(bindings)} verified bots "
                f"({sorted(b.self_id for b in bindings)}); pass bot_self_id explicitly"
            )
        logger.warning(message)
        return False, None, message, -3

    # --------------------------------------------------------------- dispatch

    async def call_action(
        self,
        action: str,
        bot_self_id: str | None = None,
        *,
        platform_id: str | None = None,
        **params: Any,
    ) -> tuple[bool, dict[str, Any] | None, str, int]:
        """Invoke an action with timeout protection, multi-bot routing, and normalization.

        Args:
            action: OneBot v11 action name.
            bot_self_id: Verified bot QQ used for routing (fail closed if unknown).
            platform_id: Optional platform instance id to disambiguate two
                connections that use the same QQ account.
            **params: OneBot action parameters.

        Returns:
            (success: bool, data: dict | None, error_msg: str, error_code: int)
        """
        binding = await self._resolve_verified(bot_self_id, platform_id)
        if binding is None:
            return self._fail_closed(bot_self_id, platform_id)

        call_action = _resolve_call_action(binding.client)
        if call_action is None:
            self.invalidate_bot_cache(binding.self_id, binding.platform_id)
            message = f"{ERR_INVALID_BOT_CLIENT}: platform {binding.platform_id!r} has no call_action"
            logger.error(message)
            return False, None, message, -3

        # ``self_id`` is the aiocqhttp routing key: it selects the reverse-WS
        # client of this bot instead of whichever connection happens to be first.
        # The verified QQ always wins over a caller-supplied ``self_id``.
        requested_self_id = params.pop("self_id", None)
        if requested_self_id is not None and str(requested_self_id).strip() != binding.self_id:
            logger.warning(
                "Ignoring caller self_id=%r; verified routing self_id=%s for bot %s",
                requested_self_id,
                binding.self_id,
                binding.platform_id,
            )
        routing_params = {**params, "self_id": binding.self_id}

        try:
            raw = await asyncio.wait_for(
                call_action(action, **routing_params),
                timeout=self.timeout,
            )
        except ACTION_FAILED_ERRORS as exc:
            return normalize_onebot_response(exc.result)
        except TimeoutError:
            self.invalidate_bot_cache(binding.self_id, binding.platform_id)
            logger.warning(f"NapCat action '{action}' timed out after {self.timeout}s")
            return False, None, f"Timeout after {self.timeout}s", -1
        except Exception as e:
            self.invalidate_bot_cache(binding.self_id, binding.platform_id)
            logger.error(f"NapCat action '{action}' call failed: {e}")
            return False, None, str(e), -2

        if raw is None:
            # Empty transport response usually means the connection just dropped.
            self.invalidate_bot_cache(binding.self_id, binding.platform_id)
        return normalize_onebot_response(raw)

    async def list_bots(self) -> list[str]:
        """Return verified bot QQ ids, sorted ascending.

        Discovery is lazy: this triggers the first ``get_login_info`` pass if it
        has not happened yet.
        """
        await self._discover()
        return sorted({binding.self_id for binding in self._bindings.values()})

    async def get_group_member_info(
        self,
        group_id: str,
        user_id: str,
        bot_self_id: str | None = None,
        no_cache: bool = True,
        *,
        platform_id: str | None = None,
    ) -> tuple[bool, dict[str, Any] | None, str, int]:
        """Fetch group member info including level and role."""
        payload = {
            "group_id": int(group_id),
            "user_id": int(user_id),
            "no_cache": bool(no_cache),
        }
        return await self.call_action(
            "get_group_member_info",
            bot_self_id=bot_self_id,
            platform_id=platform_id,
            **payload,
        )

    async def send_private_msg(
        self,
        user_id: str,
        message: str,
        bot_self_id: str | None = None,
        *,
        platform_id: str | None = None,
    ) -> tuple[bool, dict[str, Any] | None, str, int]:
        """Send private message to QQ user via the verified bot account."""
        payload = {
            "user_id": int(user_id),
            "message": message,
        }
        return await self.call_action(
            "send_private_msg",
            bot_self_id=bot_self_id,
            platform_id=platform_id,
            **payload,
        )
