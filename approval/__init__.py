"""AstrBot Group Approval."""

from .audit_service import AuditService
from .auth_service import AuthorizationService
from .config import PluginConfig
from .database import Database
from .level_service import LevelService
from .napcat_client import NapCatClient
from .notification_service import NotificationService
from .reviewers import ReviewerService

__version__ = "0.1.3"
__status__ = "Development / 调试开发版"
__author__ = "Maxwell233"

__all__ = [
    "AuditService",
    "AuthorizationService",
    "Database",
    "LevelService",
    "NapCatClient",
    "NotificationService",
    "PluginConfig",
    "ReviewerService",
]
