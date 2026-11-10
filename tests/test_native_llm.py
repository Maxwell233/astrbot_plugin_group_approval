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
        await guard.uninstall()


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
        await guard.uninstall()


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
        await guard.uninstall()


@pytest.mark.parametrize("allowed", [False, True])
async def test_real_third_party_stage_preserves_authorized_stream(monkeypatch, allowed):
    from unittest.mock import AsyncMock

    from astrbot.core.message.message_event_result import ResultContentType
    from astrbot.core.pipeline.process_stage.method.agent_sub_stages import third_party
    from astrbot.core.star.context import Context

    from approval.native_llm import NativeLLMGuard

    steps = []

    class Runtime:
        def __class_getitem__(cls, _):
            return cls

        async def reset(self, *, request, streaming, **kwargs):
            assert request.prompt == "你好"
            assert streaming
            steps.append("reset")

        async def step_until_done(self, *, max_step):
            steps.append("run")
            for part in ["第一段", "第二段"]:
                yield SimpleNamespace(type="streaming_delta", data={"chain": MessageChain().message(part)})

        async def close(self):
            steps.append("close")

        def done(self):
            return "close" in steps

        def get_final_llm_resp(self):
            return None

    async def check(event):
        steps.append("check")
        return None if allowed else "未授权"

    monkeypatch.setattr(third_party, "DifyAgentRunner", Runtime)
    monkeypatch.setattr(third_party.Metric, "upload", AsyncMock())
    monkeypatch.setattr(SessionServiceManager, "should_process_llm_request", AsyncMock(return_value=True))
    context = SimpleNamespace(
        astrbot_config={
            "wake_prefix": [],
            "provider_settings": {"enable": True, "wake_prefix": "", "streaming_response": True,
                                  "unsupported_streaming_strategy": "stream"},
            "agent_runner": {"runner_type": "dify"},
        },
        plugin_manager=SimpleNamespace(context=object.__new__(Context)),
    )
    stage = AgentRequestSubStage()
    await stage.initialize(context)
    assert isinstance(stage.agent_sub_stage, third_party.ThirdPartyAgentSubStage)
    event = _make_event("你好")
    monkeypatch.setattr(event, "send", AsyncMock())
    guard = NativeLLMGuard(check)
    guard.install()
    try:
        stream = []
        async for _ in stage.process(event):
            result = event.get_result()
            assert result.result_content_type == ResultContentType.STREAMING_RESULT
            stream.extend([chain.chain[0].text async for chain in result.async_stream])
        assert stream == (["第一段", "第二段"] if allowed else [])
        assert steps == (["check", "reset", "run", "close"] if allowed else ["check"])
        assert event.send.await_count == (0 if allowed else 1)
        if allowed:
            assert event.get_result().result_content_type == ResultContentType.STREAMING_FINISH
    finally:
        await guard.uninstall()


@pytest.mark.parametrize("allowed", [False, True])
async def test_real_local_stage_enters_agent_builder_only_after_authorization(monkeypatch, allowed):
    from unittest.mock import AsyncMock

    from astrbot.core.pipeline.process_stage.method.agent_sub_stages import internal

    from approval.native_llm import NativeLLMGuard

    calls = []

    async def check(event):
        calls.append("check")
        return None if allowed else "未授权"

    async def build_agent(**kwargs):
        calls.append("build")

    monkeypatch.setattr(internal, "build_main_agent", build_agent)
    monkeypatch.setattr(SessionServiceManager, "should_process_llm_request", AsyncMock(return_value=True))
    config = {
        "agent_runner": {"runner_type": "local"},
        "wake_prefix": [],
        "provider_settings": {"enable": True, "wake_prefix": "", "streaming_response": True,
                              "unsupported_streaming_strategy": "stream"},
    }
    ctx = SimpleNamespace(conversation_manager=object(), get_config=lambda **kwargs: config)
    stage = AgentRequestSubStage()
    await stage.initialize(SimpleNamespace(astrbot_config=config, plugin_manager=SimpleNamespace(context=ctx)))
    assert isinstance(stage.agent_sub_stage, internal.InternalAgentSubStage)
    event = _make_event()
    monkeypatch.setattr(event, "send", AsyncMock())
    guard = NativeLLMGuard(check)
    guard.install()
    try:
        assert [step async for step in stage.process(event)] == []
        assert calls == (["check", "build"] if allowed else ["check"])
        assert event.send.await_count == (0 if allowed else 1)
    finally:
        await guard.uninstall()
