"""REAL AstrBot framework integration tests: handler precedence and stop_event scope.

These tests run only in an environment where the real AstrBot package is installed
(``.venv-astrbot`` has AstrBot 4.29.4). They are skipped in the lightweight unit-test venv:

    .venv/bin/python -m pytest tests/ -q                 # this file is skipped
    .venv-astrbot/bin/python -m pytest tests/test_framework_handler_order.py -q

What is REAL here: the AstrBot decorators, the global ``star_handlers_registry``, the
actual ``extras_configs["priority"]`` values, the registry's dispatch order, a real
``AstrMessageEvent`` and its real ``stop_event()``/``is_stopped()``, and the plugin's command registration. The proxy gate tests preserve
regression evidence for the old layout.

What is still simulated: the pipeline loop itself. It is reproduced from the verified
AstrBot source (``astrbot/core/pipeline/process_stage/method/star_request.py:39-41,88-89``:
break before invoking a handler when the event is stopped, and again after each handler).
This is documented as a framework-registry integration test, NOT a full-pipeline test.
"""

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

GATE_PRIORITY = 50


# ---------------------------------------------------------------------------
# Handlers registered with the REAL decorators (module-level, so metadata.handler is
# directly callable without a class instance).
# ---------------------------------------------------------------------------
@filter.event_message_type(filter.EventMessageType.ALL, priority=100)
async def patched_moderation_100(event: AstrMessageEvent):
    event.set_extra("moderation_ran", True)


@filter.event_message_type(filter.EventMessageType.ALL, priority=50)
async def proxy_gate_50(event: AstrMessageEvent):
    if event.get_extra("deny", False):
        event.stop_event()


@filter.event_message_type(filter.EventMessageType.ALL)
async def stock_moderation_default(event: AstrMessageEvent):
    event.set_extra("stock_moderation_ran", True)


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


def _index_of(handlers, handler_name: str) -> int:
    for i, h in enumerate(handlers):
        if h.handler_name == handler_name:
            return i
    raise AssertionError(f"{handler_name} not registered in the real registry")


def _stored_priority(handler_name: str) -> int:
    for h in star_handlers_registry:
        if h.handler_name == handler_name:
            return int(h.extras_configs["priority"])
    raise AssertionError(f"{handler_name} not registered")


async def _run_real_dispatch_loop(handlers, event):
    """Reproduce AstrBot's StarRequestSubStage loop (break before AND after each handler)."""
    ran = []
    for handler in handlers:
        if event.is_stopped():  # star_request.py:39-41
            break
        result = handler.handler(event)
        if hasattr(result, "__aiter__"):
            async for _ in result:
                if event.is_stopped():
                    break
        elif hasattr(result, "__await__"):
            await result
        ran.append(handler.handler_name)
        if event.is_stopped():  # star_request.py:88-89
            break
    return ran


def test_registry_stores_real_priority_from_decorator():
    """Priority is extras_configs['priority'], descending, not a fake .priority attribute."""
    assert _stored_priority("patched_moderation_100") == 100
    assert _stored_priority("proxy_gate_50") == 50
    assert _stored_priority("stock_moderation_default") == 0

    handlers = _adapter_handlers_in_dispatch_order()
    assert _index_of(handlers, "patched_moderation_100") < _index_of(handlers, "proxy_gate_50")
    assert _index_of(handlers, "proxy_gate_50") < _index_of(handlers, "ordinary_business_default")



def test_plugin_registers_only_commands_and_no_global_gate():
    handlers = _adapter_handlers_in_dispatch_order()
    assert all(h.handler_name != "on_message_gate" for h in handlers)
    assert {"cmd_apply_group", "cmd_apply_private", "cmd_ga"} <= {h.handler_name for h in handlers}


def test_patched_moderation_runs_before_gate_and_business_never_runs():
    """The supported layout: moderation(100) -> gate(50) -> business(0)."""
    handlers = [
        h
        for h in _adapter_handlers_in_dispatch_order()
        if h.handler_name in ("patched_moderation_100", "proxy_gate_50", "ordinary_business_default")
    ]
    event = _make_event()
    event.set_extra("deny", True)
    ran = asyncio.run(_run_real_dispatch_loop(handlers, event))

    assert event.get_extra("moderation_ran") is True, "moderation must run before the gate"
    assert event.is_stopped() is True
    assert ran == ["patched_moderation_100", "proxy_gate_50"]
    assert "ordinary_business_default" not in ran
    assert event.get_extra("business_ran") is None


def test_stock_group_guardian_priority_0_is_preempted_by_the_gate():
    """Reproduces the P0 condition: with STOCK upstream GroupGuardian (priority 0) the gate
    at 50 runs first and stop_event() prevents moderation from running at all."""
    handlers = [
        h
        for h in _adapter_handlers_in_dispatch_order()
        if h.handler_name in ("proxy_gate_50", "stock_moderation_default", "ordinary_business_default")
    ]
    event = _make_event()
    event.set_extra("deny", True)

    ran = asyncio.run(_run_real_dispatch_loop(handlers, event))

    assert ran[0] == "proxy_gate_50"
    assert "stock_moderation_default" not in ran, "stock moderation expected to be pre-empted"
    assert event.get_extra("stock_moderation_ran") is None


def test_stop_event_suppresses_business_even_at_same_priority():
    """stop_event() breaks the loop for ALL remaining handlers, regardless of priority."""
    handlers = [h for h in _adapter_handlers_in_dispatch_order() if h.handler_name == "ordinary_business_default"]
    assert handlers
    event = _make_event(stop=True)  # already stopped before the loop
    ran = asyncio.run(_run_real_dispatch_loop(handlers, event))
    assert ran == []
    assert event.get_extra("business_ran") is None


# ---------------------------------------------------------------------------
# End-to-end gate behaviour under the real AstrBot event object
# ---------------------------------------------------------------------------
async def _make_plugin(tmp_path):
    import main as plugin_main

    class _Tools:
        @staticmethod
        def get_data_dir():
            return tmp_path

    plugin_main.StarTools = _Tools
    plugin = plugin_main.GroupApprovalPlugin(
        context=None,
        config={
            "enabled": True,
            "default_policy": "deny",
            "notifications": {"retry_enabled": False},
        },
    )
    await plugin.initialize()
    return plugin



async def _verified(*args):
    return True



async def test_native_check_denies_without_stopping_message(tmp_path, monkeypatch):
    plugin = await _make_plugin(tmp_path)
    try:
        monkeypatch.setattr(plugin.napcat, "has_verified_bot", _verified)
        event = _make_event("/jrys")
        assert "尚无" in await plugin._check_native_chat(event)
        assert not event.is_stopped()
    finally:
        await plugin.terminate()



async def test_admission_command_skips_only_implicit_llm(tmp_path, monkeypatch):
    plugin = await _make_plugin(tmp_path)
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
    plugin = await _make_plugin(tmp_path)
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



async def test_non_onebot_event_skips_registered_commands(tmp_path):
    plugin = await _make_plugin(tmp_path)
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
    plugin = await _make_plugin(tmp_path)
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

    plugin = await _make_plugin(tmp_path)
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
            # Deliver after the SDK starts waiting for this sequence.
            asyncio.get_running_loop().call_soon(ResultStore.add, {
                "status": "ok", "retcode": 0, "data": result, "echo": request["echo"],
            })

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
    del transport._wsr_api_clients["10001"]
    assert not await client.verify_bot("10001")
    assert await client.verify_bot("20002")
