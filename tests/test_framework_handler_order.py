"""REAL AstrBot framework integration tests: handler precedence and stop_event scope.

These tests run only in an environment where the real AstrBot package is installed
(``.venv-astrbot`` has AstrBot 4.29.4). They are skipped in the lightweight unit-test venv:

    .venv/bin/python -m pytest tests/ -q                 # this file is skipped
    .venv-astrbot/bin/python -m pytest tests/test_framework_handler_order.py -q

What is REAL here: the AstrBot decorators, the global ``star_handlers_registry``, the
actual ``extras_configs["priority"]`` values, the registry's dispatch order, a real
``AstrMessageEvent`` and its real ``stop_event()``/``is_stopped()``, and the plugin's own
gate handler registration (imported from ``main``).

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
from astrbot.core.platform.astrbot_message import AstrBotMessage, Group, MessageMember  # noqa: E402
from astrbot.core.platform.message_type import MessageType  # noqa: E402
from astrbot.core.platform.platform_metadata import PlatformMetadata  # noqa: E402
from astrbot.core.star.star_handler import EventType, star_handlers_registry  # noqa: E402

from main import GroupApprovalPlugin  # noqa: E402

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


def _make_event(message_str: str = "hello", stop: bool = False) -> AstrMessageEvent:
    msg = AstrBotMessage()
    msg.type = MessageType.GROUP_MESSAGE
    msg.self_id = "11111"
    msg.session_id = "10001"
    msg.message_id = "m1"
    msg.group = Group(group_id="10001")
    msg.sender = MessageMember(user_id="30001", nickname="tester")
    msg.message = []
    msg.message_str = message_str
    meta = PlatformMetadata(name="aiocqhttp", description="test", id="test-platform")
    event = AstrMessageEvent(
        message_str=message_str,
        message_obj=msg,
        platform_meta=meta,
        session_id="10001",
    )
    if stop:
        event.stop_event()
    return event


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


def test_plugin_gate_is_registered_at_priority_50_in_real_registry():
    handlers = _adapter_handlers_in_dispatch_order()
    gate = next(h for h in handlers if h.handler_name == "on_message_gate")
    assert int(gate.extras_configs["priority"]) == GATE_PRIORITY
    assert gate.handler_full_name.endswith("on_message_gate")


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


def _gate_result(event, plugin):
    """Drain the real plugin gate async generator the way AstrBot does."""
    import asyncio

    async def _run():
        replies = []
        async for item in plugin.on_message_gate(event):
            replies.append(item)
        return replies

    return asyncio.run(_run())


def test_real_gate_blocks_unauthorized_user_end_to_end(tmp_path):
    import asyncio

    plugin = asyncio.run(_make_plugin(tmp_path))
    try:
        # Untargeted plain text is blocked silently (anti-spam design): stopped, no reply.
        silent = _make_event("请帮我写一段代码")
        silent.is_at_or_wake_command = False
        silent_replies = _gate_result(silent, plugin)
        assert silent.is_stopped() is True
        assert silent_replies == []

        # A prefixed command gets an explicit rejection notice.
        event = _make_event("/jrys")
        event.is_at_or_wake_command = False
        replies = _gate_result(event, plugin)
        assert event.is_stopped() is True
        assert replies, "gate must yield the rejection notice for a prefixed request"
        assert "权限拦截" in str(replies[0])
    finally:
        asyncio.run(plugin.terminate())


def test_real_gate_executes_admission_command_and_stops_event(tmp_path):
    """An admission command is executed by the gate itself, never forwarded to handlers."""
    import asyncio

    plugin = asyncio.run(_make_plugin(tmp_path))
    try:
        event = _make_event("我的权限")
        event.is_at_or_wake_command = True
        replies = _gate_result(event, plugin)
        assert event.is_stopped() is True, "gate must stop the event after executing the command"
        assert replies
        # CommandDispatcher.handle_my_status actually ran (the group has no policy yet).
        assert "群 10001" in str(replies[0])
    finally:
        asyncio.run(plugin.terminate())


def test_real_gate_blocks_unauthorized_but_allows_granted_user(tmp_path):
    import asyncio
    import time

    plugin = asyncio.run(_make_plugin(tmp_path))
    try:
        now = int(time.time())
        asyncio.run(
            plugin.db.execute(
                "INSERT INTO group_policies (bot_self_id, group_id, enabled, min_level, "
                "recheck_interval_seconds, created_at, updated_at) VALUES "
                "('11111', '10001', 1, 10, 300, ?, ?)",
                (now, now),
            )
        )
        asyncio.run(
            plugin.db.execute(
                "INSERT INTO grants (id, bot_self_id, group_id, user_id, status, approved_by, "
                "approved_at, expires_at, last_verified_level, last_verified_at) VALUES "
                "('G1', '11111', '10001', '30001', 'ACTIVE', 'test', ?, 0, 20, ?)",
                (now, now),
            )
        )

        allowed_event = _make_event("请帮我写一段代码")
        _gate_result(allowed_event, plugin)
        assert allowed_event.is_stopped() is False, "granted user must reach business handlers"

        other_user = _make_event("请帮我写一段代码")
        other_user.message_obj.sender = MessageMember(user_id="30002", nickname="other")
        _gate_result(other_user, plugin)
        assert other_user.is_stopped() is True, "a different unauthorized user is still blocked"
    finally:
        asyncio.run(plugin.terminate())


def test_non_onebot_event_skips_registered_gate(tmp_path):
    plugin = asyncio.run(_make_plugin(tmp_path))
    try:
        event = _make_event("!help")
        event.platform_meta.name = "telegram"
        gate = next(h for h in _adapter_handlers_in_dispatch_order() if h.handler_name == "on_message_gate")
        assert not all(item.filter(event, {}) for item in gate.event_filters)
        _gate_result(event, plugin)
        assert not event.is_stopped()
    finally:
        asyncio.run(plugin.terminate())


def test_diagnostics_does_not_mistake_a_high_priority_sibling_for_main(monkeypatch):
    from copy import copy
    from types import SimpleNamespace

    from astrbot.core.star.star import star_map

    from approval.diagnostics import HandlerDiagnostics

    module = "data.plugins.astrbot_plugin_group_guardian.main"
    gate = next(h for h in star_handlers_registry if h.handler_name == "on_message_gate")
    monkeypatch.setitem(star_map, gate.handler_module_path, SimpleNamespace(
        activated=True, name="astrbot_plugin_group_approval",
    ))
    metadata = SimpleNamespace(activated=True, name="astrbot_plugin_group_guardian")
    monkeypatch.setitem(star_map, module, metadata)
    sibling = copy(next(h for h in star_handlers_registry if h.handler_name == "patched_moderation_100"))
    sibling.handler_module_path = module
    sibling.handler_full_name = f"{module}_other_handler"
    main_handler = copy(next(h for h in star_handlers_registry if h.handler_name == "stock_moderation_default"))
    main_handler.handler_module_path = module
    main_handler.handler_name = "_handle_message"
    main_handler.handler_full_name = f"{module}__handle_message"
    from astrbot.core.star.filter.platform_adapter_type import PlatformAdapterTypeFilter

    main_handler.event_filters = [*main_handler.event_filters, PlatformAdapterTypeFilter("aiocqhttp")]
    star_handlers_registry.append(sibling)
    star_handlers_registry.append(main_handler)
    assert HandlerDiagnostics().diagnose_handlers()["moderation_precedence"] == "CONFLICT"
    star_handlers_registry._handlers.remove(main_handler)
    main_handler.extras_configs = {"priority": 100}
    star_handlers_registry.append(main_handler)
    assert HandlerDiagnostics().diagnose_handlers()["moderation_precedence"] == "VERIFIED"
    metadata.activated = False
    assert HandlerDiagnostics().diagnose_handlers()["moderation_precedence"] == "DISABLED"
    star_handlers_registry._handlers.remove(main_handler)
    metadata.activated = True
    assert HandlerDiagnostics().diagnose_handlers()["moderation_precedence"] == "UNVERIFIED"


def test_passive_capture_waits_for_admission_decision(tmp_path):
    plugin = asyncio.run(_make_plugin(tmp_path))
    try:
        captured = []
        denied = _make_event("!help")
        denied.set_extra("group_approval.livingmemory_capture", lambda: captured.append("denied"))
        _gate_result(denied, plugin)
        assert denied.is_stopped()
        assert not captured

        allowed = _make_event("hello")
        plugin.plugin_config.owner_qq_ids.append(allowed.get_sender_id())
        plugin.plugin_config.default_policy = "allow"
        allowed.set_extra("group_approval.livingmemory_capture", lambda: captured.append("allowed"))
        _gate_result(allowed, plugin)
        assert captured == ["allowed"]
    finally:
        asyncio.run(plugin.terminate())


async def test_shipped_guardian_decorators_register_priority_and_run_in_real_stage(monkeypatch, tmp_path):
    from copy import copy
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
        gate = copy(next(h for h in star_handlers_registry if h.handler_name == "on_message_gate"))
        gate.handler = plugin.on_message_gate
        business = next(h for h in star_handlers_registry if h.handler_name == "ordinary_business_default")
        for handler in (guardian, gate, business):
            monkeypatch.setitem(star_map, handler.handler_module_path, SimpleNamespace(
                activated=True, name=handler.handler_module_path, display_name="",
            ))
        event = _make_event()
        event.set_extra("activated_handlers", [
            gate if h.handler_name == "on_message_gate" else h
            for h in _adapter_handlers_in_dispatch_order()
            if h in (guardian, business) or h.handler_name == "on_message_gate"
        ])
        async for _ in StarRequestSubStage().process(event):
            pass
        assert event.get_extra("moderation_ran") is True
        assert event.is_stopped()
        assert event.get_extra("business_ran") is None
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
