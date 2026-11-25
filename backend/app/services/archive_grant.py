"""归档版本授权服务的兼容导入路径。

权威实现位于 :mod:`app.services.archive_access`；这里保留常见模块命名，方便
按 ``ArchiveGrantService`` 定位授权能力。
"""
from __future__ import annotations

from app.services.archive_access import ArchiveAccessService, archive_access


class ArchiveGrantService(ArchiveAccessService):
    MODULE = "archive_grant"


archive_grant_service = ArchiveGrantService()

__all__ = ["ArchiveGrantService", "archive_grant_service", "archive_access"]
