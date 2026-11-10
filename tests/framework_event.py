"""A real AstrBot event fixture shared by framework component tests."""

from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, Group, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata


def _make_event(message_str: str = "hello", stop: bool = False) -> AstrMessageEvent:
    msg = AstrBotMessage()
    msg.type = MessageType.GROUP_MESSAGE
    msg.self_id = "11111"
    msg.session_id = "10001"
    msg.message_id = "m1"
    msg.group = Group(group_id="10001")
    msg.sender = MessageMember(user_id="30001", nickname="tester")
    msg.message = []
    msg.message_str = message_str
    meta = PlatformMetadata(name="aiocqhttp", description="test", id="test-platform")
    event = AstrMessageEvent(
        message_str=message_str,
        message_obj=msg,
        platform_meta=meta,
        session_id="10001",
    )
    if stop:
        event.stop_event()
    return event
