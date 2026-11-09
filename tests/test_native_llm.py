"""Real AstrBot ProcessStage/AgentRequestSubStage, with a recording runtime."""

from types import SimpleNamespace

import pytest

pytest.importorskip("astrbot")

from astrbot.core.message.message_event_result import MessageChain  # noqa: E402
from astrbot.core.pipeline.process_stage.stage import AgentRequestSubStage, ProcessStage  # noqa: E402
from astrbot.core.star.session_llm_manager import SessionServiceManager  # noqa: E402

from tests.framework_event import _make_event  # noqa: E402


@pytest.mark.parametrize("plugin_request", [False, True])
async def test_native_gate_preserves_plugin_provider_requests(monkeypatch, plugin_request):
    from approval.native_llm import NativeLLMGuard

    requests = []
    prompts = []
    checks = []

    class StarStage:
        async def process(self, event):
            event.set_extra("plugin_ran", True)
            if plugin_request:
                yield event.request_llm("plugin prompt")

    class Runtime:
        async def process(self, event, wake_prefix):
            req = event.get_extra("provider_request")
            requests.append(req.prompt if req else "native")
            yield None

    async def eligible(_event):
        return True

    async def deny(event):
        checks.append(event)
        return "需要 LLM 授权"

    async def send(message: MessageChain):
        prompts.append(message)

    monkeypatch.setattr(SessionServiceManager, "should_process_llm_request", eligible)
    stage = ProcessStage()
    stage.ctx = SimpleNamespace(astrbot_config={"provider_settings": {"enable": True}})
    stage.star_request_sub_stage = StarStage()
    stage.agent_sub_stage = AgentRequestSubStage()
    stage.agent_sub_stage.ctx = stage.ctx
    stage.agent_sub_stage.config = {}
    stage.agent_sub_stage.prov_wake_prefix = ""
    stage.agent_sub_stage.agent_sub_stage = Runtime()
    event = _make_event()
    event.is_at_or_wake_command = True
    event.set_extra("activated_handlers", [object()])
    monkeypatch.setattr(event, "send", send)

    guard = NativeLLMGuard(deny)
    guard.install()
    try:
        async for _ in stage.process(event):
            pass
        assert event.get_extra("plugin_ran")
        assert not event.is_stopped()
        assert requests == (["plugin prompt"] if plugin_request else [])
        assert checks == ([] if plugin_request else [event])
        assert len(prompts) == (0 if plugin_request else 1)
    finally:
        guard.uninstall()


@pytest.mark.parametrize(("message", "private", "allowed"), [
    ("@Bot 你好", False, False),
    ("bot 你好", False, False),
    ("你好", True, False),
    ("", False, False),
    ("", True, True),
])
async def test_native_chat_checks_once_before_any_runtime_or_history(monkeypatch, message, private, allowed):
    from astrbot.core.message.components import Image, Record
    from astrbot.core.platform.message_type import MessageType

    from approval.native_llm import NativeLLMGuard

    checked, notices, runtime_steps = [], [], []

    class Runtime:
        async def process(self, event, wake_prefix):
            for step in ("history", "provider", "tool", "provider", "stream"):
                runtime_steps.append(step)
                yield None

    async def check(event):
        checked.append(event.is_private_chat())
        return None if allowed else "未授权"

    async def send(message):
        notices.append(message)

    async def eligible(event):
        return True

    monkeypatch.setattr(SessionServiceManager, "should_process_llm_request", eligible)
    event = _make_event(message)
    if private:
        event.message_obj.type = MessageType.FRIEND_MESSAGE
        event.session.message_type = MessageType.FRIEND_MESSAGE
    if not message:
        event.message_obj.message = [Image(file="not-read.png"), Record(file="not-read.wav")]
    monkeypatch.setattr(event, "send", send)
    event.is_at_or_wake_command = True
    stage = ProcessStage()
    stage.ctx = SimpleNamespace(astrbot_config={"provider_settings": {"enable": True}})
    stage.agent_sub_stage = AgentRequestSubStage()
    stage.agent_sub_stage.ctx = stage.ctx
    stage.agent_sub_stage.config = {}
    stage.agent_sub_stage.prov_wake_prefix = ""
    stage.agent_sub_stage.agent_sub_stage = Runtime()
    guard = NativeLLMGuard(check)
    guard.install()
    try:
        for _ in range(2):
            async for _ in stage.process(event):
                pass
        assert checked == [private]
        assert len(notices) == int(not allowed)
        assert runtime_steps == (["history", "provider", "tool", "provider", "stream"] if allowed else [])
    finally:
        guard.uninstall()


@pytest.mark.parametrize("name", ["签到", "塔罗", "今日小猪", "GroupGuardian", "独立模型插件"])
async def test_independent_handler_runs_before_default_llm_denial(monkeypatch, name):
    from astrbot.core.pipeline.process_stage.method.star_request import StarRequestSubStage
    from astrbot.core.star.star import star_map
    from astrbot.core.star.star_handler import EventType, StarHandlerMetadata

    from approval.native_llm import NativeLLMGuard

    order = []
    module = "fixture.independent"

    async def plugin_handler(event):
        order.append(name)
        if name == "独立模型插件":
            # Represents a plugin calling a provider directly, with no pipeline request.
            order.append("plugin-owned provider")
        if name != "GroupGuardian":
            await event.send(MessageChain().message(name))

    handler = StarHandlerMetadata(
        event_type=EventType.AdapterMessageEvent,
        handler_full_name=f"{module}_plugin_handler", handler_name="plugin_handler",
        handler_module_path=module, handler=plugin_handler, event_filters=[],
    )
    monkeypatch.setitem(star_map, module, SimpleNamespace(name=name, display_name=""))

    async def send(chain):
        event._has_send_oper = True

    async def check(event):
        order.append("LLM check")
        return "未授权"

    event = _make_event(name)
    event.is_at_or_wake_command = True
    event.set_extra("activated_handlers", [handler])
    monkeypatch.setattr(event, "send", send)
    stage = ProcessStage()
    stage.ctx = SimpleNamespace(astrbot_config={"provider_settings": {"enable": True}})
    stage.star_request_sub_stage = StarRequestSubStage()
    stage.agent_sub_stage = AgentRequestSubStage()
    guard = NativeLLMGuard(check)
    guard.install()
    try:
        async for _ in stage.process(event):
            pass
        expected = {
            "GroupGuardian": [name, "LLM check"],
            "独立模型插件": [name, "plugin-owned provider"],
        }
        assert order == expected.get(name, [name])
        assert not event.is_stopped()
    finally:
        guard.uninstall()
