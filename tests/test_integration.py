"""Production event callback and command dispatch on the scoped service."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import main
from tests.test_scope_service import BOT, OWNER, PRIMARY, USER


@pytest.fixture
def integration(scoped):
    service, napcat = scoped
    plugin = object.__new__(main.GroupApprovalPlugin)
    plugin.plugin_config = service.config
    plugin.scopes = service
    plugin.commands = main.ScopeCommands(service)
    plugin.napcat = SimpleNamespace(has_verified_bot=AsyncMock(return_value=True))
    return plugin, napcat


def event(*, bot=BOT, private=False, user=USER, text="hello", post_type="message"):
    return SimpleNamespace(
        message_obj={"post_type": post_type}, message_str=text,
        get_self_id=lambda: bot, get_sender_id=lambda: user, get_platform_id=lambda: "adapter-1",
        get_platform_name=lambda: "aiocqhttp", get_group_id=lambda: "" if private else PRIMARY,
        is_private_chat=lambda: private, should_call_llm=lambda flag: None,
    )


async def test_unauthorized_user_rejected_only_at_native_admission(integration):
    plugin, _ = integration
    native = event(text="/jrys")
    assert "尚无" in await plugin._check_native_chat(native)
    assert not hasattr(plugin, "on_message_gate")


async def test_admission_commands_are_handled_without_llm_grant(integration):
    plugin, _ = integration
    assert "已开通" in await plugin._command(event(text="申请群聊"), "申请群聊")
    assert "ACTIVE" in await plugin._command(event(text="我的权限"), "我的权限")


async def test_authorized_user_is_allowed(integration):
    plugin, _ = integration
    await plugin.scopes.apply(BOT, USER, "GROUP", "")
    assert await plugin._check_native_chat(event()) is None
    assert await plugin._check_native_chat(event(private=True))


@pytest.mark.parametrize("bot", ["", "default", "²"])
async def test_missing_or_invalid_bot_identity_fails_closed(integration, bot):
    plugin, _ = integration
    assert "身份" in await plugin._check_native_chat(event(bot=bot))
    plugin.napcat.has_verified_bot.assert_not_awaited()


async def test_unverified_platform_identity_fails_closed(integration):
    plugin, _ = integration
    plugin.napcat.has_verified_bot.return_value = False
    assert "身份" in await plugin._check_native_chat(event())
    plugin.napcat.has_verified_bot.assert_awaited_once_with(BOT, "adapter-1")


async def test_private_chat_and_admin_commands(integration):
    plugin, _ = integration
    assert "私聊" in await plugin._check_native_chat(event(private=True))
    assert "主群" in await plugin._command(event(private=True, user=OWNER, text="ga status"), "ga")


async def test_fail_closed_on_napcat_disconnect(integration):
    plugin, napcat = integration
    await plugin.scopes.configure(BOT, OWNER, cache_ttl=0)
    await plugin.scopes.apply(BOT, USER, "GROUP", "")
    napcat.network_error_on_next = True
    assert "无法核验" in await plugin._check_native_chat(event())
    assert (await plugin.scopes.grant(BOT, USER, "GROUP"))["status"] == "ACTIVE"
