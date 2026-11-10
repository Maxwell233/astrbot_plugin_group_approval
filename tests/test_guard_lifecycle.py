"""Lifecycle regression tests on AstrBot's real AgentRequestSubStage class."""

import asyncio
import gc
import importlib
import weakref
from functools import wraps

import pytest

pytest.importorskip("astrbot")

from astrbot.core.pipeline.process_stage.stage import AgentRequestSubStage  # noqa: E402

import approval.native_llm as native  # noqa: E402
from tests.framework_event import _make_event  # noqa: E402


@pytest.fixture
def stage(monkeypatch):
    """Keep the real framework class, with a recording downstream generator."""
    calls = []

    async def downstream(self, event):
        calls.append(event)
        yield "part 1"
        yield "part 2"

    monkeypatch.setattr(AgentRequestSubStage, "process", downstream)
    # Independent framework instance lifecycle for each test.
    monkeypatch.setattr(AgentRequestSubStage, native.STATE_ATTRIBUTE, None, raising=False)
    return AgentRequestSubStage(), downstream, calls


async def drain(stage):
    return [part async for part in stage.process(_make_event())]


async def test_duplicate_registration_and_repeated_reload(stage):
    agent, downstream, calls = stage
    checks = []

    async def check(event):
        checks.append(event)

    guard = native.NativeLLMGuard(check)
    guard.install()
    wrapper = AgentRequestSubStage.process
    try:
        guard.install()
        assert AgentRequestSubStage.process is wrapper
        with pytest.raises(RuntimeError, match="already"):
            native.NativeLLMGuard(check).install()
    finally:
        await guard.uninstall()
    assert AgentRequestSubStage.process is downstream
    for _ in range(3):
        guard = importlib.reload(native).NativeLLMGuard(check)
        guard.install()
        assert AgentRequestSubStage.process is wrapper
        assert await drain(agent) == ["part 1", "part 2"]
        await guard.uninstall()
        assert AgentRequestSubStage.process is downstream
    assert len(checks) == len(calls) == 3


@pytest.mark.parametrize("advertise", [True, False])
async def test_outer_wrapper_survives_unload_and_reattaches_without_retaining_owner(stage, monkeypatch, advertise):
    agent, downstream, calls = stage
    checks = []

    class Owner:
        async def check(self, event):
            checks.append(event)

    owner = Owner()
    old_ref = weakref.ref(owner)
    guard = native.NativeLLMGuard(owner.check)
    guard.install()
    inner = AgentRequestSubStage.process

    async def outer(self, event):
        async for part in inner(self, event):
            yield part

    if advertise:
        outer = wraps(inner)(outer)
    monkeypatch.setattr(AgentRequestSubStage, "process", outer)
    await drain(agent)
    assert len(checks) == 1
    await guard.uninstall()
    del owner, guard
    gc.collect()
    assert old_ref() is None
    assert AgentRequestSubStage.process is outer
    await drain(agent)
    assert len(checks) == 1

    replacement = Owner()
    guard = importlib.reload(native).NativeLLMGuard(replacement.check)
    guard.install()
    try:
        await drain(agent)
        assert AgentRequestSubStage.process is outer
        assert len(checks) == 2
        assert len(calls) == 3
    finally:
        await guard.uninstall()
        monkeypatch.setattr(AgentRequestSubStage, "process", downstream)


async def test_preexisting_wrapper_restored_and_replacement_reported(stage, monkeypatch, caplog):
    agent, downstream, calls = stage

    @wraps(downstream)
    async def earlier(self, event):
        async for part in downstream(self, event):
            yield part

    monkeypatch.setattr(AgentRequestSubStage, "process", earlier)

    async def check(event):
        return None

    guard = native.NativeLLMGuard(check)
    guard.install()
    try:
        assert await drain(agent) == ["part 1", "part 2"]
        assert len(calls) == 1
    finally:
        await guard.uninstall()
    assert AgentRequestSubStage.process is earlier
    guard.install()

    @wraps(AgentRequestSubStage.process)
    async def replacement(self, event):
        yield "unrelated"

    monkeypatch.setattr(AgentRequestSubStage, "process", replacement)
    try:
        assert guard.status != "ACTIVE"
        assert not guard.installed
        assert "cannot verify" in caplog.text
    finally:
        await guard.uninstall()
    assert AgentRequestSubStage.process is replacement


async def test_uninstall_drains_checks_and_denies_new_native_work(stage):
    agent, downstream, calls = stage
    entered, release = asyncio.Event(), asyncio.Event()
    db_closed = False
    checked = []

    async def check(event):
        checked.append(event)
        entered.set()
        await release.wait()
        assert not db_closed

    guard = native.NativeLLMGuard(check)
    guard.install()
    request = asyncio.create_task(drain(agent))
    await entered.wait()

    async def close():
        nonlocal db_closed
        await guard.uninstall()
        db_closed = True

    closing = asyncio.create_task(close())
    await asyncio.sleep(0)
    try:
        assert guard.status == "DRAINING"
        assert not db_closed
        assert await drain(agent) == []
        assert len(checked) == 1
    finally:
        release.set()
        await asyncio.gather(request, closing)
    assert db_closed
    assert AgentRequestSubStage.process is downstream
    assert not calls


async def test_plugin_filter_cannot_skip_guard_and_cancel_propagates(stage):
    agent, _, calls = stage
    ready = asyncio.Event()

    async def check(event):
        ready.set()
        await asyncio.Future()

    guard = native.NativeLLMGuard(check)
    guard.install()
    try:
        event = _make_event()
        event.plugins_name = ["unrelated_plugin"]

        async def consume():
            return [part async for part in agent.process(event)]

        task = asyncio.create_task(consume())
        await ready.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not calls
    finally:
        await guard.uninstall()
