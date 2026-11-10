"""Lifecycle regression tests on AstrBot's real AgentRequestSubStage class."""

import asyncio
import gc
import importlib
import weakref
from functools import wraps
from types import SimpleNamespace

import pytest

pytest.importorskip("astrbot")

from astrbot.core.pipeline.process_stage.stage import AgentRequestSubStage  # noqa: E402
from astrbot.core.star.context import Context  # noqa: E402

import approval.native_llm as native  # noqa: E402
import main  # noqa: E402
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
        async for part in earlier(self, event):
            yield part

    monkeypatch.setattr(AgentRequestSubStage, "process", replacement)
    try:
        assert await drain(agent) == ["part 1", "part 2"]
        assert len(calls) == 2
        assert guard.status == "EXTERNAL_METHOD_UNVERIFIED"
        assert not guard.installed
        assert "cannot verify" in caplog.text
    finally:
        await guard.uninstall()
    assert AgentRequestSubStage.process is replacement


async def test_uninstall_drains_checks_before_closing_database(stage, tmp_path, monkeypatch):
    agent, downstream, calls = stage
    entered, release = asyncio.Event(), asyncio.Event()
    checked = []
    monkeypatch.setattr(main, "StarTools", SimpleNamespace(get_data_dir=lambda *_: tmp_path))

    async def check(self, event):
        checked.append(event)
        entered.set()
        await release.wait()
        assert self.db._shared_conn is not None
        assert await self.db.fetchone("SELECT 1 FROM grants") is None

    monkeypatch.setattr(main.GroupApprovalPlugin, "_check_native_chat", check)
    plugin = main.GroupApprovalPlugin(config={"notifications": {"retry_enabled": False}})
    await plugin.initialize()
    request = asyncio.create_task(drain(agent))
    await entered.wait()

    closing = asyncio.create_task(plugin.terminate())
    await asyncio.sleep(0)
    try:
        assert plugin.native_guard.status == "DRAINING"
        assert plugin.db._shared_conn is not None
        assert await drain(agent) == []
        assert len(checked) == 1
    finally:
        release.set()
        request_result, _ = await asyncio.gather(request, closing)
    assert request_result == []
    assert plugin.db._shared_conn is None
    assert AgentRequestSubStage.process is downstream
    assert not calls


async def test_unload_unregisters_and_drains_dashboard_routes(tmp_path, monkeypatch):
    context = object.__new__(Context)
    context.registered_web_apis = []
    monkeypatch.setattr(main, "StarTools", SimpleNamespace(get_data_dir=lambda *_: tmp_path))
    monkeypatch.setattr(main, "json_response", lambda data: data)
    plugin = main.GroupApprovalPlugin(
        context, config={"enabled": False, "notifications": {"retry_enabled": False}},
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def state():
        entered.set()
        await release.wait()
        assert plugin.db._shared_conn is not None
        return {"status": "ok"}

    async def replacement():
        return {}

    monkeypatch.setattr(plugin, "_web_state", state)
    await plugin.initialize()
    routes = {entry[0]: entry[1] for entry in context.registered_web_apis}
    state_handler = routes[f"/{main.PLUGIN_NAME}/scope/state"]
    bots_path = f"/{main.PLUGIN_NAME}/bots"
    context.register_web_api(bots_path, replacement, ["GET"], "replacement")
    request = asyncio.create_task(state_handler())
    await asyncio.wait_for(entered.wait(), 2)
    closing = asyncio.create_task(plugin.terminate())
    await asyncio.sleep(0)
    try:
        assert context.registered_web_apis == [(bots_path, replacement, ["GET"], "replacement")]
        assert not closing.done()
        assert (await asyncio.wait_for(state_handler(), 2))["retcode"] == 503
    finally:
        release.set()
        await asyncio.gather(request, closing, return_exceptions=True)
    assert request.result() == {"status": "ok"}
    assert plugin.db._shared_conn is None
    assert (await state_handler())["retcode"] == 503
    assert plugin.db._shared_conn is None


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
