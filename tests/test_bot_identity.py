"""Bot identity and multi-bot OneBot routing tests.

The only fake here is the network transport (``FakeCQHttpClient``); platform
instances/managers/contexts are real objects implementing the official AstrBot
shapes documented in ``audit/astrbot-facts.md`` §f/§i.  No ``MagicMock`` of
``.self_id`` is used — the transport records the exact ``self_id`` kwarg that
AstrBot's aiocqhttp adapter would use for routing.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from approval.level_service import LevelService
from approval.napcat_client import (
    ERR_AMBIGUOUS_BOT,
    ERR_NO_VERIFIED_BOT,
    ERR_UNKNOWN_BOT_SELF_ID,
    NapCatClient,
)

# --------------------------------------------------------------------------
# Real-shaped fixtures / fake transport
# --------------------------------------------------------------------------


@dataclass
class FakePlatformMetadata:
    """Mirror of AstrBot ``PlatformMetadata`` (name/id only)."""

    name: str
    id: str


class FakeCQHttpClient:
    """Fake network transport exposing ``call_action`` like aiocqhttp's CQHttp.

    ``get_login_info`` verifies the account; other actions record the exact
    ``self_id`` routing kwarg and return configurable OneBot responses.
    """

    def __init__(
        self,
        login_self_id: str,
        *,
        login_wrapped: bool = True,
        login_error: bool = False,
    ) -> None:
        self.login_self_id = str(login_self_id)
        self.login_wrapped = login_wrapped
        self.login_error = login_error
        self.login_calls = 0
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.action_calls: list[tuple[str, dict[str, Any]]] = []
        self.responses: dict[str, Any] = {}
        self.default_response: Any = {"status": "ok", "retcode": 0, "data": {"message_id": 123}}
        self.raise_on_next_action: BaseException | None = None
        self.delay_on_action = 0.0

    async def call_action(self, action: str, **params: Any) -> Any:
        self.calls.append((action, dict(params)))
        if action == "get_login_info":
            self.login_calls += 1
            if self.login_error:
                raise ConnectionError("reverse websocket disconnected")
            data = {"user_id": int(self.login_self_id), "nickname": f"bot-{self.login_self_id}"}
            if self.login_wrapped:
                return {"status": "ok", "retcode": 0, "data": data}
            return data

        self.action_calls.append((action, dict(params)))
        if self.delay_on_action:
            await asyncio.sleep(self.delay_on_action)
        if self.raise_on_next_action is not None:
            exc = self.raise_on_next_action
            self.raise_on_next_action = None
            raise exc
        response = self.responses.get(action, self.default_response)
        return response

    def actions_of(self, action: str) -> list[dict[str, Any]]:
        return [params for called_action, params in self.action_calls if called_action == action]


class FakePlatformInstance:
    """Mirror of an AstrBot platform adapter instance (``meta``/``get_client``)."""

    def __init__(self, platform_id: str, client: Any, name: str = "aiocqhttp") -> None:
        self._meta = FakePlatformMetadata(name=name, id=platform_id)
        self._client = client
        self.config = {"id": platform_id}

    def meta(self) -> FakePlatformMetadata:
        return self._meta

    def get_client(self) -> Any:
        return self._client


class FakePlatformManager:
    def __init__(self, instances: list[FakePlatformInstance]) -> None:
        self.platform_insts = list(instances)

    def get_insts(self) -> list[FakePlatformInstance]:
        return list(self.platform_insts)


class FakeContext:
    def __init__(self, instances: list[FakePlatformInstance]) -> None:
        self.platform_manager = FakePlatformManager(instances)


class FakeEvent:
    """Mirror of the AiocqhttpMessageEvent surface used by the plugin."""

    def __init__(
        self,
        self_id: str,
        platform_id: str | None = None,
        bot: Any = None,
    ) -> None:
        self._self_id = self_id
        self._platform_id = platform_id
        if bot is not None:
            self.bot = bot

    def get_self_id(self) -> str:
        return self._self_id

    def get_platform_id(self):
        return self._platform_id


def make_client(
    instances: list[FakePlatformInstance],
    *,
    timeout: float = 1.0,
) -> NapCatClient:
    client = NapCatClient(timeout=timeout)
    client.set_context(FakeContext(instances))
    return client


def assert_routed_self_id(fake_client: FakeCQHttpClient, expected_self_id: str) -> None:
    assert fake_client.action_calls, "expected at least one non-login action call"
    for action, params in fake_client.action_calls:
        assert params.get("self_id") == expected_self_id, (
            f"action {action} routed with {params.get('self_id')!r}, expected {expected_self_id!r}"
        )
        assert "bot_self_id" not in params


# --------------------------------------------------------------------------
# Response normalization shapes
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wrapped_onebot_response_is_success():
    transport = FakeCQHttpClient("10001")
    client = make_client([FakePlatformInstance("inst-a", transport)])

    ok, data, err, retcode = await client.send_private_msg("30001", "hello", bot_self_id="10001")

    assert (ok, err, retcode) == (True, "", 0)
    assert data == {"message_id": 123}
    assert_routed_self_id(transport, "10001")


@pytest.mark.asyncio
async def test_already_unwrapped_response_is_success():
    transport = FakeCQHttpClient("10001")
    transport.responses["send_private_msg"] = {"message_id": 77, "status": "sent"}
    client = make_client([FakePlatformInstance("inst-a", transport)])

    ok, data, err, retcode = await client.send_private_msg("30001", "hi", bot_self_id="10001")

    assert (ok, err, retcode) == (True, "", 0)
    assert data["message_id"] == 77
    assert_routed_self_id(transport, "10001")


@pytest.mark.asyncio
async def test_error_response_returns_failure_tuple():
    transport = FakeCQHttpClient("10001")
    transport.responses["send_group_msg"] = {
        "status": "failed",
        "retcode": 10001,
        "msg": "group not found",
    }
    client = make_client([FakePlatformInstance("inst-a", transport)])

    ok, data, err, retcode = await client.send_group_msg("90001", "hi", bot_self_id="10001")

    assert ok is False
    assert data is None
    assert err == "group not found"
    assert retcode == 10001


@pytest.mark.asyncio
async def test_odd_success_receipt_is_success():
    transport = FakeCQHttpClient("10001")
    transport.responses["send_private_msg"] = {"status": "ok", "retcode": 0, "data": 12345}
    client = make_client([FakePlatformInstance("inst-a", transport)])

    ok, data, _err, retcode = await client.send_private_msg("30001", "hi", bot_self_id="10001")
    assert ok is True and retcode == 0
    assert data == {"value": 12345}

    # A raw message_id-only receipt is also success.
    transport.responses["send_private_msg"] = {"message_id": 0}
    ok_none, data_none, _e, _r = await client.send_private_msg("30001", "hi", bot_self_id="10001")
    assert ok_none is True
    assert data_none["message_id"] == 0


# --------------------------------------------------------------------------
# Verified discovery / routing
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_bots_route_to_correct_instance_and_forward_self_id():
    transport_a = FakeCQHttpClient("10001")
    transport_b = FakeCQHttpClient("20002")
    client = make_client(
        [
            FakePlatformInstance("inst-a", transport_a),
            FakePlatformInstance("inst-b", transport_b),
        ]
    )

    ok, _data, _err, _retcode = await client.send_private_msg("30001", "hi", bot_self_id="20002")

    assert ok is True
    assert_routed_self_id(transport_b, "20002")
    # The other bot's transport must never receive the action.
    assert transport_a.actions_of("send_private_msg") == []
    assert await client.list_bots() == ["10001", "20002"]


@pytest.mark.asyncio
async def test_unknown_bot_self_id_fails_closed_without_fallback():
    transport_a = FakeCQHttpClient("10001")
    transport_b = FakeCQHttpClient("20002")
    client = make_client(
        [
            FakePlatformInstance("inst-a", transport_a),
            FakePlatformInstance("inst-b", transport_b),
        ]
    )

    ok, data, err, retcode = await client.send_private_msg("30001", "hi", bot_self_id="99999")

    assert ok is False
    assert data is None
    assert err.startswith(ERR_UNKNOWN_BOT_SELF_ID)
    assert retcode == -3
    assert transport_a.action_calls == []
    assert transport_b.action_calls == []


@pytest.mark.asyncio
async def test_untargeted_send_with_multiple_bots_fails_closed():
    client = make_client(
        [
            FakePlatformInstance("inst-a", FakeCQHttpClient("10001")),
            FakePlatformInstance("inst-b", FakeCQHttpClient("20002")),
        ]
    )

    ok, data, err, retcode = await client.send_group_msg("90001", "hi")

    assert (ok, data, retcode) == (False, None, -3)
    assert err.startswith(ERR_AMBIGUOUS_BOT)


@pytest.mark.asyncio
async def test_untargeted_send_with_single_bot_succeeds():
    transport = FakeCQHttpClient("10001")
    client = make_client([FakePlatformInstance("inst-a", transport)])

    ok, _data, _err, _retcode = await client.send_private_msg("30001", "hi")

    assert ok is True
    assert_routed_self_id(transport, "10001")


@pytest.mark.asyncio
async def test_non_aiocqhttp_instances_are_ignored():
    telegram_client = FakeCQHttpClient("55555")
    client = make_client(
        [FakePlatformInstance("tg-1", telegram_client, name="telegram")],
    )

    assert await client.list_bots() == []
    ok, _data, err, retcode = await client.send_private_msg("30001", "hi", bot_self_id="55555")
    assert (ok, retcode) == (False, -3)
    assert err.startswith(ERR_UNKNOWN_BOT_SELF_ID)
    assert telegram_client.action_calls == []


@pytest.mark.asyncio
async def test_list_bots_returns_only_verified_bots():
    transport_ok = FakeCQHttpClient("10001")
    transport_broken = FakeCQHttpClient("20002", login_error=True)
    instance_ok = FakePlatformInstance("inst-a", transport_ok)
    instance_broken = FakePlatformInstance("inst-b", transport_broken)
    client = make_client([instance_ok, instance_broken])

    assert await client.list_bots() == ["10001"]

    # Reconnect: once get_login_info recovers, discovery picks it up again.
    transport_broken.login_error = False
    client.invalidate_bot_cache(platform_id="inst-b")
    assert await client.list_bots() == ["10001", "20002"]


@pytest.mark.asyncio
async def test_same_account_on_two_platform_ids_can_be_disambiguated():
    transport_a = FakeCQHttpClient("10001")
    transport_b = FakeCQHttpClient("10001")  # same QQ, different platform instance
    client = make_client(
        [
            FakePlatformInstance("inst-a", transport_a),
            FakePlatformInstance("inst-b", transport_b),
        ]
    )

    assert await client.list_bots() == ["10001"]

    ok, _data, _err, _retcode = await client.send_private_msg(
        "30001", "hi", bot_self_id="10001", platform_id="inst-b"
    )
    assert ok is True
    assert_routed_self_id(transport_b, "10001")
    assert transport_a.action_calls == []


# --------------------------------------------------------------------------
# Reconnect / failure handling
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_fails_closed_then_reverifies():
    transport = FakeCQHttpClient("10001")
    transport.delay_on_action = 0.25
    client = make_client([FakePlatformInstance("inst-a", transport)], timeout=0.05)

    ok, data, err, retcode = await client.send_private_msg("30001", "hi", bot_self_id="10001")
    assert (ok, data, retcode) == (False, None, -1)
    assert "Timeout" in err
    logins_after_timeout = transport.login_calls

    transport.delay_on_action = 0.0
    ok2, _data2, _err2, _retcode2 = await client.send_private_msg("30001", "hi", bot_self_id="10001")
    assert ok2 is True
    # Timeout invalidated the binding -> get_login_info ran again before retry.
    assert transport.login_calls > logins_after_timeout
    assert_routed_self_id(transport, "10001")


@pytest.mark.asyncio
async def test_disconnect_invalidates_and_rebinds_reconnected_instance():
    transport_old = FakeCQHttpClient("10001")
    instance = FakePlatformInstance("inst-a", transport_old)
    client = make_client([instance])

    assert (await client.send_private_msg("30001", "hi", bot_self_id="10001"))[0] is True
    assert transport_old.login_calls == 1

    # Simulate a dropped reverse-WS: transport raises -> binding invalidated.
    transport_old.raise_on_next_action = ConnectionError("reverse websocket closed")
    ok, _data, err, retcode = await client.send_private_msg("30001", "hi", bot_self_id="10001")
    assert (ok, retcode) == (False, -2)
    assert "reverse websocket closed" in err

    # Reconnect: same platform id now exposes a brand-new client object.
    transport_new = FakeCQHttpClient("10001")
    instance._client = transport_new
    ok2, _data2, _err2, _retcode2 = await client.send_private_msg("30001", "hi", bot_self_id="10001")
    assert ok2 is True
    # The new connection had to be verified again, and the action went to it.
    assert transport_new.login_calls == 1
    assert_routed_self_id(transport_new, "10001")


# --------------------------------------------------------------------------
# Event-attested identity
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remember_event_identity_via_platform_id():
    transport = FakeCQHttpClient("30003", login_error=True)  # get_login_info unavailable
    instance = FakePlatformInstance("inst-a", transport)
    client = make_client([instance])
    event = FakeEvent(self_id="30003", platform_id="inst-a", bot=transport)

    learned = await client.remember_event_identity(event)
    assert learned == "30003"

    ok, _data, _err, _retcode = await client.send_private_msg("40004", "hi", bot_self_id="30003")
    assert ok is True
    assert_routed_self_id(transport, "30003")


@pytest.mark.asyncio
async def test_remember_event_identity_via_event_bot_without_platform_id():
    transport = FakeCQHttpClient("30003", login_error=True)
    client = make_client([])  # no discoverable platform instances
    event = FakeEvent(self_id="30003", platform_id=None, bot=transport)

    assert await client.remember_event_identity(event) == "30003"

    ok, _data, _err, _retcode = await client.send_group_msg("90001", "hi", bot_self_id="30003")
    assert ok is True
    assert_routed_self_id(transport, "30003")


@pytest.mark.asyncio
async def test_remember_event_identity_rejects_missing_self_id():
    client = make_client([])
    assert await client.remember_event_identity(FakeEvent(self_id="", platform_id="inst-a")) is None
    assert await client.remember_event_identity(None) is None


# --------------------------------------------------------------------------
# LevelService integration (real client + fake transport)
# --------------------------------------------------------------------------


def make_member_transport() -> FakeCQHttpClient:
    transport = FakeCQHttpClient("10001")
    transport.responses["get_group_member_info"] = {
        "status": "ok",
        "retcode": 0,
        "data": {
            "group_id": 10001,
            "user_id": 20001,
            "level": "18",
            "role": "member",
        },
    }
    return transport


@pytest.mark.asyncio
async def test_level_service_forwards_bot_self_id_and_parses_level():
    transport = make_member_transport()
    client = make_client([FakePlatformInstance("inst-a", transport)])
    service = LevelService(client)

    result = await service.get_group_member_level("10001", "10001", "20001")

    assert result.success is True
    assert result.level == 18
    assert result.is_member is True
    assert_routed_self_id(transport, "10001")


@pytest.mark.asyncio
async def test_level_service_fails_closed_on_unknown_bot():
    transport = make_member_transport()
    client = make_client([FakePlatformInstance("inst-a", transport)])
    service = LevelService(client)

    result = await service.get_group_member_level("99999", "10001", "20001")

    assert result.success is False
    assert result.is_member is False
    assert result.error_code == ERR_UNKNOWN_BOT_SELF_ID
    assert transport.actions_of("get_group_member_info") == []


@pytest.mark.asyncio
async def test_no_context_fails_closed():
    client = NapCatClient()
    assert await client.list_bots() == []
    ok, data, err, retcode = await client.send_private_msg("30001", "hi", bot_self_id="10001")
    assert (ok, data, retcode) == (False, None, -3)
    assert err.startswith(ERR_UNKNOWN_BOT_SELF_ID)
    assert ERR_NO_VERIFIED_BOT not in err


@pytest.mark.asyncio
async def test_unwrapped_login_info_is_accepted():
    # aiocqhttp's call_action returns the ``data`` field directly (unwrapped).
    transport = FakeCQHttpClient("10001", login_wrapped=False)
    client = make_client([FakePlatformInstance("inst-a", transport)])

    ok, _data, _err, _retcode = await client.send_private_msg("30001", "hi", bot_self_id="10001")

    assert ok is True
    assert await client.list_bots() == ["10001"]
    assert_routed_self_id(transport, "10001")


@pytest.mark.asyncio
async def test_discovery_is_lazy_until_first_use():
    transport = FakeCQHttpClient("10001")
    client = make_client([FakePlatformInstance("inst-a", transport)])

    # Constructing/setting context must not perform network calls.
    assert transport.login_calls == 0
    assert transport.calls == []

    assert await client.list_bots() == ["10001"]
    assert transport.login_calls == 1


@pytest.mark.asyncio
async def test_verified_self_id_wins_over_caller_supplied_self_id():
    transport = FakeCQHttpClient("10001")
    client = make_client([FakePlatformInstance("inst-a", transport)])

    ok, _data, _err, _retcode = await client.call_action(
        "send_private_msg",
        bot_self_id="10001",
        self_id="66666",  # spoof/override attempt must not change routing
        user_id=30001,
        message="hi",
    )

    assert ok is True
    assert_routed_self_id(transport, "10001")


class MultiAccountReverseClient(FakeCQHttpClient):
    def __init__(self):
        super().__init__("10001")
        self._wsr_api_clients = {"10001": object(), "20002": object()}
        self.login_mismatch = set()

    async def call_action(self, action, **params):
        target = params.get("self_id")
        if target not in self._wsr_api_clients:
            raise ConnectionError("unknown reverse WebSocket")
        if action == "get_login_info":
            return {"user_id": 10001 if target in self.login_mismatch else int(target)}
        return await super().call_action(action, **params)


async def test_discover_route_and_disconnect_two_bots_on_one_instance():
    transport = MultiAccountReverseClient()
    client = make_client([FakePlatformInstance("shared", transport)])
    assert await client.list_bots() == ["10001", "20002"]
    for bot in ("10001", "20002"):
        assert (await client.send_private_msg("30001", "test", bot_self_id=bot))[0]
    assert [params["self_id"] for params in transport.actions_of("send_private_msg")] == ["10001", "20002"]
    del transport._wsr_api_clients["20002"]
    assert await client.list_bots() == ["10001"]
    assert not (await client.send_private_msg("30001", "test", bot_self_id="20002"))[0]
    transport._wsr_api_clients["20002"] = object()
    assert await client.list_bots() == ["10001", "20002"]


async def test_reverse_login_must_match_routed_socket_identity():
    transport = MultiAccountReverseClient()
    transport.login_mismatch.add("20002")
    client = make_client([FakePlatformInstance("shared", transport)])
    assert await client.list_bots() == ["10001"]
    assert not (await client.send_private_msg("30001", "test", bot_self_id="20002"))[0]
