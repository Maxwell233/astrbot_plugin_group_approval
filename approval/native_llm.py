"""Process-wide native-chat admission for AstrBot 4.29.4."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass, field
from functools import wraps
from inspect import ismethod
from typing import Any
from weakref import WeakMethod

from astrbot.api import logger

SUPPORTED_FRAMEWORK = "4.29.4"
STATE_ATTRIBUTE = "_astrbot_group_approval_native_runtime"
SUPPRESS_DENIAL_NOTICE = "group_approval.suppress_denial_notice"

Check = Callable[[Any], Awaitable[str | None]]


@dataclass
class _Registration:
    callback: Check | WeakMethod | None
    in_flight: int = 0
    drained: asyncio.Event = field(default_factory=asyncio.Event)

    def resolve(self) -> Check | None:
        return self.callback() if isinstance(self.callback, WeakMethod) else self.callback


class _Runtime:
    """Anchored to the framework class so plugin module reloads share one wrapper."""

    def __init__(self, stage, chain_type, request_type):
        self.stage = stage
        self.original = stage.process
        self.registration: _Registration | None = None
        self._last_diagnostic: str | None = None

        @wraps(self.original, updated=())
        async def guarded(instance, event):
            registration = self.registration
            if registration is not None and event.get_platform_name() == "aiocqhttp":
                if isinstance(event.get_extra("provider_request"), request_type):
                    # An explicit plugin request must not fall through to a
                    # second, implicit request on the same message.
                    event.should_call_llm(True)
                else:
                    if event.is_stopped() or event.call_llm:
                        return
                    event.should_call_llm(True)
                    if registration.callback is None:  # draining
                        return
                    notice = await self._check(registration, event)
                    if registration.callback is None:
                        return  # uninstall began while the check was suspended
                    if notice:
                        if not event.get_extra(SUPPRESS_DENIAL_NOTICE):
                            await event.send(chain_type().message(notice))
                        return
            async with aclosing(self.original(instance, event)) as generator:
                async for part in generator:
                    yield part

        self.wrapper = guarded

    async def _check(self, registration: _Registration, event) -> str | None:
        check = registration.resolve()
        if check is None:
            return "AI 聊天准入暂不可用，请稍后再试。"
        registration.in_flight += 1
        try:
            return await check(event)
        except Exception:
            logger.exception("Native chat authorization failed")
            return "暂时无法核验 AI 聊天权限，请稍后再试。"
        finally:
            registration.in_flight -= 1
            self._finish_uninstall(registration)

    def _finish_uninstall(self, registration: _Registration) -> None:
        if self.registration is not registration or registration.callback is not None or registration.in_flight:
            return
        self.registration = None
        # Preserve an external outer function, even if it no longer delegates.
        if self.stage.process is self.wrapper:
            self.stage.process = self.original
        registration.drained.set()

    def stop(self, registration: _Registration) -> None:
        if self.registration is registration:
            registration.callback = None
            self._finish_uninstall(registration)

    def status(self, registration: _Registration) -> str:
        if self.registration is not registration:
            return "INACTIVE"
        if registration.callback is None:
            return "DRAINING"
        if registration.resolve() is None:
            status = "CALLBACK_GONE"
        elif self.stage.process is self.wrapper:
            status = "ACTIVE"
        else:
            # __wrapped__ is metadata. Even a callable advertising our wrapper
            # can skip delegation, so only report verified health when direct.
            status = "EXTERNAL_METHOD_UNVERIFIED"
        if status != self._last_diagnostic:
            self._last_diagnostic = status
            if status != "ACTIVE":
                logger.error("Native LLM admission: cannot verify the current callback/method (%s)", status)
        return status


async def _monitor(state: _Runtime, registration: _Registration) -> None:
    while state.registration is registration and registration.callback is not None:
        state.status(registration)
        await asyncio.sleep(5)


class NativeLLMGuard:
    """A handle to one live registration. Does not own the framework wrapper."""

    def __init__(self, check: Check):
        self.check = check
        self._state: _Runtime | None = None
        self._registration: _Registration | None = None
        self._monitor: asyncio.Task | None = None

    @property
    def status(self) -> str:
        if self._state is None or self._registration is None:
            return "INACTIVE"
        return self._state.status(self._registration)

    @property
    def installed(self) -> bool:
        return self.status == "ACTIVE"

    def install(self) -> None:
        from astrbot import __version__
        from astrbot.core.message.message_event_result import MessageChain
        from astrbot.core.pipeline.process_stage.stage import AgentRequestSubStage
        from astrbot.core.provider.entities import ProviderRequest

        if __version__ != SUPPORTED_FRAMEWORK:
            raise RuntimeError(f"Native LLM admission requires AstrBot {SUPPORTED_FRAMEWORK}, got {__version__}")
        state = getattr(AgentRequestSubStage, STATE_ATTRIBUTE, None)
        if state is None:
            state = _Runtime(AgentRequestSubStage, MessageChain, ProviderRequest)
            setattr(AgentRequestSubStage, STATE_ATTRIBUTE, state)
        current = state.registration
        if current is not None:
            if current is self._registration and current.callback is not None:
                return
            raise RuntimeError("Group Approval native guard already has an active or draining registration")

        loop = asyncio.get_running_loop()
        callback = WeakMethod(self.check) if ismethod(self.check) else self.check
        registration = _Registration(callback)
        state.registration = registration
        # If another plugin owns the class method, leave it alone. An outer
        # wrapper still calling ours will pick up this new registration.
        if AgentRequestSubStage.process is state.original:
            AgentRequestSubStage.process = state.wrapper
        self._state, self._registration = state, registration
        state.status(registration)
        self._monitor = loop.create_task(_monitor(state, registration))

    async def uninstall(self) -> None:
        state, registration = self._state, self._registration
        if state is None or registration is None:
            return
        state.stop(registration)
        monitor = self._monitor
        if monitor is not None:
            monitor.cancel()
        # The database belongs to the caller, which closes it only after this
        # wait. Cancellation of a caller does not cancel any authorization.
        await registration.drained.wait()
        if monitor is not None:
            await asyncio.gather(monitor, return_exceptions=True)
        self._state = self._registration = self._monitor = None
