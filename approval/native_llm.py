"""AstrBot 4.29.4 compatibility adapter for the default Agent entry point."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)
SUPPORTED_FRAMEWORK = "4.29.4"


class NativeLLMGuard:
    """Wrap the Agent substage reached by both branches of ProcessStage.

    The branch receiving an explicit ProviderRequest belongs to a plugin. The
    implicit branch starts the framework's native chat, before either runtime
    prepares media/history or invokes a provider.
    """

    def __init__(self, check: Callable[[Any], Awaitable[str | None]]):
        self.check = check
        self.stage: Any = None
        self.original: Any = None
        self.wrapper: Any = None

    @property
    def installed(self) -> bool:
        return self.stage is not None and self.stage.process is self.wrapper

    def install(self) -> None:
        from astrbot import __version__
        from astrbot.core.message.message_event_result import MessageChain
        from astrbot.core.pipeline.process_stage.stage import AgentRequestSubStage
        from astrbot.core.provider.entities import ProviderRequest

        if self.installed:
            return
        if __version__ != SUPPORTED_FRAMEWORK:
            raise RuntimeError(f"Native LLM admission requires AstrBot {SUPPORTED_FRAMEWORK}, got {__version__}")
        original = AgentRequestSubStage.process
        if getattr(original, "_group_approval_guard", False):
            raise RuntimeError("Group Approval native guard is already installed")

        @wraps(original)
        async def guarded(stage, event):
            if event.get_platform_name() == "aiocqhttp":
                if isinstance(event.get_extra("provider_request"), ProviderRequest):
                    # Even a plugin request that produces no reply must not fall
                    # through to a second, implicit native chat on the same event.
                    event.should_call_llm(True)
                else:
                    if event.is_stopped() or event.call_llm:
                        return
                    event.should_call_llm(True)
                    try:
                        notice = await self.check(event)
                    except Exception:
                        logger.exception("Native chat authorization failed")
                        notice = "暂时无法核验 AI 聊天权限，请稍后再试。"
                    if notice:
                        await event.send(MessageChain().message(notice))
                        return
            async for result in original(stage, event):
                yield result

        guarded._group_approval_guard = True
        self.stage, self.original, self.wrapper = AgentRequestSubStage, original, guarded
        AgentRequestSubStage.process = guarded

    def uninstall(self) -> None:
        if self.installed:
            self.stage.process = self.original
        self.stage = self.original = self.wrapper = None
