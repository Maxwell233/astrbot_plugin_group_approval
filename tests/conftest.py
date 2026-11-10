"""Pytest configuration and common fixtures."""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio

from approval.config import PluginConfig
from approval.database import Database
from approval.napcat_client import NapCatClient


class MockNapCatClient(NapCatClient):
    """Configurable mock for NapCat OneBot v11 client."""

    def __init__(self) -> None:
        super().__init__()
        # Key: (group_id, user_id) -> dict(level=..., role=..., is_member=..., error=...)
        self.verified_bot_ids = ["10001"]
        self.members: dict[tuple[str, str], dict[str, Any]] = {}
        self.sent_private_messages: list[tuple[str, str]] = []
        self.sent_group_messages: list[tuple[str, str]] = []
        self.timeout_on_next = False
        self.network_error_on_next = False

    def set_member(
        self,
        group_id: str,
        user_id: str,
        level: Any = 10,
        role: str = "member",
        is_member: bool = True,
        qq_level: int = 50,
    ) -> None:
        self.members[(str(group_id), str(user_id))] = {
            "level": level,
            "role": role,
            "is_member": is_member,
            "qq_level": qq_level,
        }

    async def get_group_member_info(
        self,
        group_id: str,
        user_id: str,
        bot_self_id: str | None = None,
        no_cache: bool = True,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if self.timeout_on_next:
            self.timeout_on_next = False
            await asyncio.sleep(0.01)
            return {"status": "failed", "retcode": -1, "msg": "Timeout"}

        if self.network_error_on_next:
            self.network_error_on_next = False
            return {"status": "failed", "retcode": 100, "msg": "Network failure"}

        key = (str(group_id), str(user_id))
        if key not in self.members:
            return {
                "status": "failed",
                "retcode": 10001,
                "msg": "Group member not found or left group",
            }

        m = self.members[key]
        if not m.get("is_member", True):
            return {
                "status": "failed",
                "retcode": 10001,
                "msg": "User is not a member",
            }

        return {
            "status": "ok",
            "retcode": 0,
            "data": {
                "group_id": int(group_id),
                "user_id": int(user_id),
                "nickname": f"User_{user_id}",
                "card": "",
                "level": m.get("level"),
                "role": m.get("role", "member"),
                "qq_level": m.get("qq_level", 50),
            },
        }

    async def list_bots(self) -> list[str]:
        return list(self.verified_bot_ids)

    async def verify_bot(self, bot_self_id: str) -> bool:
        return bot_self_id in self.verified_bot_ids

    async def send_private_msg(
        self,
        user_id: str,
        message: str,
        bot_self_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.sent_private_messages.append((str(user_id), message))
        return {"status": "ok", "retcode": 0, "data": {"message_id": 123}}

    async def send_group_msg(
        self,
        group_id: str,
        message: str,
        bot_self_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.sent_group_messages.append((str(group_id), message))
        return {"status": "ok", "retcode": 0, "data": {"message_id": 456}}


@pytest.fixture
def mock_napcat() -> MockNapCatClient:
    client = MockNapCatClient()
    # Default test setup
    client.set_member(group_id="10001", user_id="20001", level="10", role="member")
    client.set_member(group_id="10001", user_id="20002", level="9", role="member")
    client.set_member(group_id="10001", user_id="20003", level="25", role="member")
    return client


@pytest_asyncio.fixture
async def temp_db() -> AsyncGenerator[Database, None]:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_path = tf.name

    db = Database(db_path)
    await db.init()
    yield db
    await db.close()

    # Clean up temp files
    for p in (db_path, f"{db_path}-wal", f"{db_path}-shm"):
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass


@pytest.fixture
def test_config() -> PluginConfig:
    return PluginConfig.from_dict({"owner_qq_ids": ["99999"], "reviewer_qq_ids": ["88888"]})


@pytest_asyncio.fixture
async def scoped(temp_db, mock_napcat, test_config):
    from approval import AuditService, LevelService, NotificationService, ReviewerService
    from approval.scope_service import ScopeService

    service = ScopeService(
        temp_db, LevelService(mock_napcat), AuditService(temp_db),
        NotificationService(temp_db, mock_napcat), test_config,
        ReviewerService(temp_db, test_config),
    )
    for bot in ("11111", "22222"):
        mock_napcat.set_member("10001", bot, "30")
    mock_napcat.set_member("10001", "20001", "15")
    await service.configure(
        "11111", "99999", primary_group_id="10001", policies={"GROUP": {"approval_required": False}},
    )
    return service, mock_napcat


@pytest.fixture
async def dashboard_plugin(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import main
    from approval.auth_service import AuthorizationService

    monkeypatch.setattr(main, "StarTools", SimpleNamespace(get_data_dir=lambda: tmp_path))
    monkeypatch.setattr(main, "ASTRBOT_AVAILABLE", True)
    instance = main.GroupApprovalPlugin(
        config={"enabled": False, "notifications": {"retry_enabled": False}}
    )
    instance.auth = AuthorizationService(lambda: "dashboard-admin")

    async def verify(bot):
        return bot in {"11111", "22222"}

    async def list_bots():
        return ["11111", "22222"]

    monkeypatch.setattr(instance.napcat, "verify_bot", verify)
    monkeypatch.setattr(instance.napcat, "list_bots", list_bots)
    await instance.initialize()
    try:
        yield instance
    finally:
        await instance.terminate()
