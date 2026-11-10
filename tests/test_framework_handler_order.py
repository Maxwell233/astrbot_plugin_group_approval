"""Command dispatch and optional moderation patch checks on real AstrBot 4.29.4."""

from __future__ import annotations

import asyncio

import pytest

astrbot = pytest.importorskip("astrbot")

from astrbot.api.event import filter  # noqa: E402
from astrbot.core.platform.astr_message_event import AstrMessageEvent  # noqa: E402
from astrbot.core.platform.astrbot_message import MessageMember  # noqa: E402
from astrbot.core.star.star_handler import EventType, star_handlers_registry  # noqa: E402

from main import GroupApprovalPlugin  # noqa: E402
from tests.framework_event import _make_event  # noqa: E402


@filter.event_message_type(filter.EventMessageType.ALL)
async def ordinary_business_default(event: AstrMessageEvent):
    event.set_extra("business_ran", True)


@pytest.fixture(autouse=True)
def isolate_handler_registry():
    """Snapshot/restore the process-global registry around each test."""
    saved_handlers = list(star_handlers_registry._handlers)
    saved_map = dict(star_handlers_registry.star_handlers_map)
    try:
        yield
    finally:
        star_handlers_registry._handlers[:] = saved_handlers
        star_handlers_registry.star_handlers_map.clear()
        star_handlers_registry.star_handlers_map.update(saved_map)


def _adapter_handlers_in_dispatch_order():
    return star_handlers_registry.get_handlers_by_event_type(
        EventType.AdapterMessageEvent, only_activated=False
    )


def test_plugin_registers_only_commands_and_no_global_gate():
    handlers = _adapter_handlers_in_dispatch_order()
    assert all(h.handler_name != "on_message_gate" for h in handlers)
    assert {"cmd_apply_group", "cmd_apply_private", "cmd_ga"} <= {h.handler_name for h in handlers}


async def _make_plugin(tmp_path, monkeypatch):
    import main as plugin_main

    class _Tools:
        @staticmethod
        def get_data_dir(plugin_name):
            return tmp_path

    monkeypatch.setattr(plugin_main, "StarTools", _Tools)
    plugin = plugin_main.GroupApprovalPlugin(
        context=None,
        config={
            "enabled": True,
            "notifications": {"retry_enabled": False},
        },
    )
    await plugin.initialize()
    return plugin


async def _verified(*args):
    return True


async def test_native_check_denies_without_stopping_message(tmp_path, monkeypatch):
    plugin = await _make_plugin(tmp_path, monkeypatch)
    try:
        monkeypatch.setattr(plugin.napcat, "has_verified_bot", _verified)
        event = _make_event("/jrys")
        assert "尚无" in await plugin._check_native_chat(event)
        assert not event.is_stopped()
    finally:
        await plugin.terminate()


async def test_admission_command_skips_only_implicit_llm(tmp_path, monkeypatch):
    plugin = await _make_plugin(tmp_path, monkeypatch)
    try:
        monkeypatch.setattr(plugin.napcat, "has_verified_bot", _verified)
        event = _make_event("我的权限")
        replies = [reply async for reply in plugin.cmd_my_status(event)]
        assert replies
        assert event.call_llm
        assert not event.is_stopped()
    finally:
        await plugin.terminate()


async def test_native_check_allows_scope_grant_and_rejects_other_user(tmp_path, monkeypatch):
    plugin = await _make_plugin(tmp_path, monkeypatch)
    try:
        monkeypatch.setattr(plugin.napcat, "has_verified_bot", _verified)
        await plugin.scopes.manual_grant(
            "11111", "30001", "GROUP", "99999", level_exempt=True, membership_exempt=True
        )
        assert await plugin._check_native_chat(_make_event()) is None
        from astrbot.core.platform.message_type import MessageType

        other_group = _make_event()
        other_group.message_obj.group.group_id = "90001"
        assert await plugin._check_native_chat(other_group) is None
        private = _make_event()
        private.message_obj.type = MessageType.FRIEND_MESSAGE
        private.session.message_type = MessageType.FRIEND_MESSAGE
        assert "私聊" in await plugin._check_native_chat(private)
        event = _make_event()
        event.message_obj.sender = MessageMember(user_id="30002", nickname="other")
        assert await plugin._check_native_chat(event)
    finally:
        await plugin.terminate()


async def test_non_onebot_event_skips_registered_commands(tmp_path, monkeypatch):
    plugin = await _make_plugin(tmp_path, monkeypatch)
    try:
        event = _make_event("我的权限")
        event.platform_meta.name = "telegram"
        command = next(h for h in _adapter_handlers_in_dispatch_order() if h.handler_name == "cmd_my_status")
        assert not all(item.filter(event, {}) for item in command.event_filters)
        assert [reply async for reply in plugin.cmd_my_status(event)] == []
        assert not event.is_stopped()
    finally:
        await plugin.terminate()


def test_plugin_has_no_legacy_priority_diagnostics_route():
    assert not hasattr(GroupApprovalPlugin, "_web_diagnostics")


async def test_native_admission_leaves_passive_capture_alone(tmp_path, monkeypatch):
    plugin = await _make_plugin(tmp_path, monkeypatch)
    try:
        monkeypatch.setattr(plugin.napcat, "has_verified_bot", _verified)
        event = _make_event("hello")
        sentinel = object()
        event.set_extra("group_approval.livingmemory_capture", sentinel)
        assert await plugin._check_native_chat(event)
        assert event.get_extra("group_approval.livingmemory_capture") is sentinel
        assert not event.is_stopped()
    finally:
        await plugin.terminate()


async def test_shipped_guardian_decorators_register_priority_and_run_in_real_stage(monkeypatch, tmp_path):
    from pathlib import Path
    from types import SimpleNamespace

    from astrbot.core.pipeline.process_stage.method.star_request import StarRequestSubStage
    from astrbot.core.star.star import star_map

    lines = Path("patches/group_guardian_priority.patch").read_text().splitlines()
    decorators = [
        line[1:].strip() for line in lines
        if line.startswith(("+", " ")) and line[1:].strip().startswith("@filter.")
    ]
    module = "data.plugins.astrbot_plugin_group_guardian.main"
    namespace = {"filter": filter, "__name__": module}
    # Real decorators from the patch, a harmless moderation body and the real process stage.
    exec("\n".join([*decorators, "async def _handle_message(event):",
                   "    event.set_extra('moderation_ran', True)"]), namespace)
    guardian = next(h for h in star_handlers_registry if h.handler_module_path == module)
    assert guardian.extras_configs["priority"] == 100

    plugin = await _make_plugin(tmp_path, monkeypatch)
    try:
        monkeypatch.setattr(plugin.napcat, "has_verified_bot", _verified)
        business = next(h for h in star_handlers_registry if h.handler_name == "ordinary_business_default")
        for handler in (guardian, business):
            monkeypatch.setitem(star_map, handler.handler_module_path, SimpleNamespace(
                activated=True, name=handler.handler_module_path, display_name="",
            ))
        event = _make_event()
        event.set_extra("activated_handlers", [
            h for h in _adapter_handlers_in_dispatch_order() if h in (guardian, business)
        ])
        async for _ in StarRequestSubStage().process(event):
            pass
        assert event.get_extra("moderation_ran") is True
        assert await plugin._check_native_chat(event)
        assert not event.is_stopped()
        assert event.get_extra("business_ran") is True
    finally:
        await plugin.terminate()


async def test_real_aiocqhttp_routes_multiple_accounts_in_one_adapter():
    import json
    from types import SimpleNamespace

    from aiocqhttp import CQHttp
    from aiocqhttp.api_impl import ResultStore

    from approval.napcat_client import NapCatClient

    class Socket:
        def __init__(self, qq):
            self.qq = qq
            self.requests = []

        async def send(self, message):
            request = json.loads(message)
            self.requests.append(request)
            result = {"user_id": int(self.qq)} if request["action"] == "get_login_info" else {"message_id": 1}
            response = {"status": "ok", "retcode": 0, "data": result, "echo": request["echo"]}
            if request["params"].get("message") == "rejected":
                response.update(status="failed", retcode=10001, message="recipient unavailable")
            # Deliver after the SDK starts waiting for this sequence.
            asyncio.get_running_loop().call_soon(ResultStore.add, response)

    transport = CQHttp()
    sockets = {qq: Socket(qq) for qq in ("10001", "20002")}
    transport._wsr_api_clients.update(sockets)
    platform = SimpleNamespace(
        meta=lambda: SimpleNamespace(name="aiocqhttp", id="shared"),
        get_client=lambda: transport,
    )
    client = NapCatClient()
    client.set_context(SimpleNamespace(platform_manager=SimpleNamespace(get_insts=lambda: [platform])))
    assert await client.list_bots() == ["10001", "20002"]
    for qq, socket in sockets.items():
        assert await client.verify_bot(qq)
        assert (await client.send_private_msg("30001", "test", bot_self_id=qq))[0]
        assert [req["action"] for req in socket.requests] == [
            "get_login_info", "get_login_info", "send_private_msg",
        ]
        assert all(req["params"]["self_id"] == qq for req in socket.requests)
    assert await client.send_private_msg("30001", "rejected", bot_self_id="20002") == (
        False, None, "recipient unavailable", 10001,
    )
    assert await client.list_bots() == ["10001", "20002"]
    assert [req["action"] for req in sockets["20002"].requests][-2:] == ["send_private_msg", "send_private_msg"]
    del transport._wsr_api_clients["10001"]
    assert not await client.verify_bot("10001")
    assert await client.verify_bot("20002")


async def test_denial_notice_cooldown_survives_restart_and_spans_sessions(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from astrbot.core.pipeline.process_stage.stage import AgentRequestSubStage
    from astrbot.core.platform.message_type import MessageType

    from approval import native_llm, rate_limit

    now = 100_000
    monkeypatch.setattr(rate_limit, "time", SimpleNamespace(time=lambda: now))
    model_requests = []

    async def downstream(self, event):
        model_requests.append(event)
        yield

    monkeypatch.setattr(AgentRequestSubStage, "process", downstream)
    monkeypatch.setattr(AgentRequestSubStage, native_llm.STATE_ATTRIBUTE, None, raising=False)
    stage = AgentRequestSubStage()

    async def try_chat(*, bot="11111", user="30001", private=False, group="10001"):
        event = _make_event()
        event.message_obj.self_id = bot
        event.message_obj.sender = MessageMember(user_id=user, nickname="tester")
        if private:
            event.message_obj.type = MessageType.FRIEND_MESSAGE
            event.session.message_type = MessageType.FRIEND_MESSAGE
        else:
            event.message_obj.group.group_id = group
        monkeypatch.setattr(event, "send", AsyncMock())
        async for _ in stage.process(event):
            pass
        assert not event.is_stopped()
        return event.send.await_count

    plugin = await _make_plugin(tmp_path, monkeypatch)
    try:
        monkeypatch.setattr(plugin.napcat, "has_verified_bot", _verified)
        assert sum(await asyncio.gather(
            try_chat(), try_chat(private=True), try_chat(group="10002")
        )) == 1
        assert await try_chat(bot="22222") == 1
        assert await try_chat(user="30002") == 1
        assert not model_requests
    finally:
        await plugin.terminate()

    plugin = await _make_plugin(tmp_path, monkeypatch)
    try:
        monkeypatch.setattr(plugin.napcat, "has_verified_bot", _verified)
        now += 24 * 60 * 60 - 1
        assert await try_chat() == 0
        now += 1
        assert await try_chat() == 1
        assert await try_chat(private=True) == 0
        assert not model_requests

        await plugin.scopes.manual_grant(
            "11111", "30001", "GROUP", "99999", level_exempt=True, membership_exempt=True
        )
        assert await try_chat() == 0
        assert len(model_requests) == 1
    finally:
        await plugin.terminate()


@pytest.mark.parametrize("wake_prefix", ["/", "!"])
async def test_private_apply_returns_status_through_real_dispatch(tmp_path, monkeypatch, wake_prefix):
    from dataclasses import replace
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from astrbot.core.pipeline.process_stage.method.star_request import StarRequestSubStage
    from astrbot.core.pipeline.process_stage.stage import ProcessStage
    from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.star import session_plugin_manager
    from astrbot.core.star.star import star_map

    plugin = await _make_plugin(tmp_path, monkeypatch)
    try:
        plugin.plugin_config.owner_qq_ids.append("30001")
        monkeypatch.setattr(plugin.napcat, "has_verified_bot", _verified)
        monkeypatch.setattr(session_plugin_manager.sp, "get_async", AsyncMock(return_value={}))
        handler = next(h for h in _adapter_handlers_in_dispatch_order() if h.handler_name == "cmd_apply_private")
        star_handlers_registry.remove(handler)
        star_handlers_registry.append(replace(handler, handler=plugin.cmd_apply_private))
        monkeypatch.setitem(star_map, handler.handler_module_path, SimpleNamespace(
            name="astrbot_plugin_group_approval", activated=True, display_name="", reserved=False,
        ))

        ctx = SimpleNamespace(
            astrbot_config={"wake_prefix": [wake_prefix], "platform_settings": {}, "admins_id": [],
                            "provider_settings": {"enable": True}},
            db_helper=None, astrbot_config_id="test",
        )
        waking = WakingCheckStage()
        await waking.initialize(ctx)
        event = _make_event("/申请私聊")
        event.message_obj.type = MessageType.FRIEND_MESSAGE
        event.session.message_type = MessageType.FRIEND_MESSAGE

        assert "Owner 原生聊天豁免" in await plugin._check_native_chat(event)

        await waking.process(event)
        assert [h.handler_name for h in event.get_extra("activated_handlers")] == ["cmd_apply_private"]

        class NoModel:
            async def process(self, event):
                pytest.fail("The application command entered the native model stage")
                yield

        stage = ProcessStage()
        stage.ctx = ctx
        stage.star_request_sub_stage = StarRequestSubStage()
        stage.agent_sub_stage = NoModel()
        replies = []
        async for _ in stage.process(event):
            replies.extend(part.text for part in event.get_result().chain)
        assert replies == ["此 Bot 尚未配置主群。"]
        assert not event.is_stopped()
        await plugin.scopes.configure("11111", "30001", owner_bypass=True)
        assert await plugin._check_native_chat(event) is None
    finally:
        await plugin.terminate()
