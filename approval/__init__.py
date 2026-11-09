"""AstrBot Plugin Group Approval (群等级准入与管理员审批)."""

__version__ = "0.1.1"
__status__ = "Development / 调试开发版"
__author__ = "Maxwell233"

from .access_service import AccessService
from .approval_service import ApprovalService
from .audit_service import AuditService
from .commands import CommandDispatcher
from .config import PluginConfig
from .database import Database
from .gate import AdmissionGate
from .level_service import LevelService
from .models import (
    AccessDecision,
    Application,
    ApplicationStatus,
    AuditLogEntry,
    BlacklistEntry,
    Grant,
    GrantStatus,
    GroupMemberLevelResult,
    GroupPolicy,
    Role,
)
from .napcat_client import NapCatClient
from .notification_service import NotificationService
from .web_api import WebApiHandler

__all__ = [
    "AccessDecision",
    "AccessService",
    "AdmissionGate",
    "Application",
    "ApplicationStatus",
    "ApprovalService",
    "AuditLogEntry",
    "AuditService",
    "BlacklistEntry",
    "CommandDispatcher",
    "Database",
    "Grant",
    "GrantStatus",
    "GroupMemberLevelResult",
    "GroupPolicy",
    "LevelService",
    "NapCatClient",
    "NotificationService",
    "PluginConfig",
    "Role",
    "WebApiHandler",
]
