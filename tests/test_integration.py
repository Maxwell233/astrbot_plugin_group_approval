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


def event(*, bot=BOT, private=False, user=USER, text="hello", message_type=None):
    return SimpleNamespace(
        message_str=text,
        get_message_type=lambda: message_type or (
            main.MessageType.FRIEND_MESSAGE if private else main.MessageType.GROUP_MESSAGE
        ),
        get_self_id=lambda: bot, get_sender_id=lambda: user, get_platform_id=lambda: "adapter-1",
        get_platform_name=lambda: "aiocqhttp", get_group_id=lambda: "" if private else PRIMARY,
        is_private_chat=lambda: private, should_call_llm=lambda flag: None, set_extra=lambda key, value: None,
    )


async def test_unauthorized_user_rejected_only_at_native_admission(integration):
    plugin, _ = integration
    native = event(text="/jrys")
    assert "尚无" in await plugin._check_native_chat(native)
    assert not hasattr(plugin, "on_message_gate")


async def test_non_chat_event_is_denied_before_identity_lookup(integration):
    plugin, _ = integration
    notice = event(message_type=main.MessageType.OTHER_MESSAGE)
    assert "会话类型" in await plugin._check_native_chat(notice)
    plugin.napcat.has_verified_bot.assert_not_awaited()


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


async def test_opt_in_owner_inheritance_uses_current_global_astrbot_admin_ids(tmp_path, monkeypatch):
    from unittest.mock import Mock

    global_config = {"admins_id": [USER, int(OWNER), "astrbot", "０１２３４", True]}
    get_config = Mock(return_value=global_config)
    context = SimpleNamespace(get_config=get_config)
    monkeypatch.setattr(main, "ASTRBOT_AVAILABLE", True)
    monkeypatch.setattr(main, "StarTools", SimpleNamespace(get_data_dir=lambda *_: tmp_path))
    plugin = main.GroupApprovalPlugin(context=context, config={"inherit_astrbot_admins": True})
    monkeypatch.setattr(plugin.napcat, "has_verified_bot", AsyncMock(return_value=True))

    await plugin.db.init()
    try:
        admin_command = event(private=True, user=USER, text="ga status")
        assert "主群" in await plugin._command(admin_command, "ga")
        assert plugin.plugin_config.is_owner(OWNER)
        assert not plugin.plugin_config.is_owner("astrbot")
        assert not plugin.plugin_config.is_owner("０１２３４")
        assert not plugin.plugin_config.is_owner("True")
        assert all(not call.args and not call.kwargs for call in get_config.call_args_list)

        chat = event(private=True, user=USER)
        assert "Owner 原生聊天豁免" in await plugin._check_native_chat(chat)
        await plugin.scopes.configure(BOT, USER, owner_bypass=True)
        assert await plugin._check_native_chat(chat) is None
        assert await plugin._check_native_chat(event(bot="22222", private=True, user=USER))

        global_config["admins_id"] = []
        admin_command.role = "admin"
        assert "需要 Owner" in await plugin._command(admin_command, "ga")
        assert "尚无" in await plugin._check_native_chat(chat)
    finally:
        await plugin.db.close()


def test_astrbot_admins_do_not_replace_explicit_owners_when_inheritance_is_disabled(tmp_path, monkeypatch):
    from unittest.mock import Mock

    get_config = Mock(return_value={"admins_id": [USER]})
    context = SimpleNamespace(get_config=get_config)
    monkeypatch.setattr(main, "ASTRBOT_AVAILABLE", True)
    monkeypatch.setattr(main, "StarTools", SimpleNamespace(get_data_dir=lambda *_: tmp_path))
    plugin = main.GroupApprovalPlugin(context=context, config={"owner_qq_ids": [OWNER]})
    assert plugin.plugin_config.is_owner(OWNER)
    assert not plugin.plugin_config.is_owner(USER)
    get_config.assert_not_called()


async def test_permission_command_displays_identity_only_in_private(integration):
    plugin, _ = integration
    private = await plugin._command(event(private=True, text="/我的权限"), "我的权限")
    assert private.splitlines()[:2] == [f"当前 Bot QQ：{BOT}", f"你的 QQ：{USER}"]
    group = await plugin._command(event(text="/我的权限"), "我的权限")
    assert BOT not in group and USER not in group
