"""归档授权服务的另一个常见命名入口。"""
from __future__ import annotations

from app.services.archive_access import ArchiveAccessService, archive_access


class ArchiveAuthorizationService(ArchiveAccessService):
    MODULE = "archive_grant"


archive_authorization_service = ArchiveAuthorizationService()

__all__ = [
    "ArchiveAuthorizationService",
    "archive_authorization_service",
    "archive_access",
]
