"""Scoped QQ commands and private administrative boundaries."""

import pytest

from approval.scope_commands import ScopeCommands
from approval.scope_service import ScopeError
from tests.test_scope_service import BOT, OTHER_BOT, OWNER, PRIMARY, REVIEWER, USER


async def test_user_commands_flow(scoped):
    service, napcat = scoped
    commands = ScopeCommands(service)
    assert "LV20" in await commands.user(BOT, USER, False, "使用规则", [], PRIMARY)
    assert "未授权" in await commands.user(BOT, USER, False, "我的权限", [], PRIMARY)
    assert "已开通" in await commands.user(BOT, USER, False, "申请使用", [], PRIMARY)
    napcat.set_member(PRIMARY, USER, "25")
    assert "申请已提交" in await commands.user(BOT, USER, True, "申请使用", ["test"], "")
    assert "PENDING" in await commands.user(BOT, USER, True, "申请状态", [], "")
    assert "已取消" in await commands.user(BOT, USER, False, "取消申请", ["私聊"], PRIMARY)
    assert await service.check_chat(BOT, USER, "GROUP") is None
    assert await service.check_chat(BOT, USER, "PRIVATE")


async def test_admin_commands_permissions(scoped):
    service, _ = scoped
    commands = ScopeCommands(service)
    assert "权限" in await commands.admin(BOT, USER, True, ["pending"])
    assert "私聊" in await commands.admin(BOT, REVIEWER, False, ["pending"])
    assert "暂无" in await commands.admin(BOT, REVIEWER, True, ["pending"])
    with pytest.raises(ScopeError, match="权限"):
        await commands.admin(BOT, REVIEWER, True, ["grant", USER, "private", "--level-exempt"])
    assert "保存" in await commands.admin(BOT, OWNER, True, ["grant", USER, "private", "--level-exempt"])
    assert await service.check_chat(BOT, USER, "PRIVATE") is None


async def test_admin_commands_cannot_expose_or_review_other_bot_applications(scoped):
    service, napcat = scoped
    napcat.set_member(PRIMARY, USER, "25")
    await service.apply(BOT, USER, "PRIVATE", PRIMARY, "private-reason")
    app = (await service.overview(BOT))["applications"][0]
    commands = ScopeCommands(service)
    assert "暂无" in await commands.admin(OTHER_BOT, REVIEWER, True, ["pending"])
    for action in ["approve", "reject"]:
        with pytest.raises(ScopeError, match="该 Bot"):
            await commands.admin(OTHER_BOT, REVIEWER, True, [action, app["id"]])
    assert (await service.application(BOT, app["id"]))["status"] == "PENDING"


async def test_group_replies_do_not_disclose_private_review_notes(scoped):
    service, napcat = scoped
    napcat.set_member(PRIMARY, USER, "25")
    await service.apply(BOT, USER, "PRIVATE", PRIMARY, "private-reason")
    app = (await service.overview(BOT))["applications"][0]
    commands = ScopeCommands(service)
    assert "private-reason" in await commands.admin(BOT, REVIEWER, True, ["pending"])
    assert "private-reason" not in await commands.admin(BOT, REVIEWER, False, ["pending"])
    await commands.admin(BOT, REVIEWER, True, ["reject", app["id"], "private-note"])
    reply = await commands.user(BOT, USER, False, "申请状态", [], PRIMARY)
    assert "请私聊" in reply and "private-note" not in reply


async def test_group_admin_mutation_does_not_publish_applicant(scoped):
    service, napcat = scoped
    napcat.set_member(PRIMARY, USER, "25")
    await service.apply(BOT, USER, "PRIVATE", PRIMARY)
    app = (await service.overview(BOT))["applications"][0]
    reply = await ScopeCommands(service).admin(BOT, REVIEWER, False, ["approve", app["id"]])
    assert "私聊" in reply and USER not in reply
    assert (await service.application(BOT, app["id"]))["status"] == "PENDING"
