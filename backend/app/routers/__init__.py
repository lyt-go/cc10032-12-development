"""业务路由汇总：授权领域接口先注册，随后是通用资源路由。"""
from __future__ import annotations

from app.routers import archive_access, resource

ROUTERS = [archive_access, resource]
