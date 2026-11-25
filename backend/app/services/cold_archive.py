"""cold_archive 领域服务：冷热数据分层归档。

业务逻辑：
1. 冷热分层：热数据保留天数和冷存储路径
2. 归档链路：cold_archive → retention（归档后的数据由保留策略管理生命周期）
3. 失败场景：冷存储路径不可达、查询透传失败、归档格式不兼容
4. 与上下游流水线：为 retention 提供归档清单

跨模块数据流：
  cold_archive → retention（冷归档清单决定保留策略的清理范围）
"""
from __future__ import annotations

import time
from typing import Any

from app.services.archive_access import archive_access
from app.services.base import BaseModuleService
from app.store import store


class ColdArchiveService(BaseModuleService):
    MODULE = "cold_archive"
    ENTITY = "归档策略"
    OPERATION_TERM = "存储"
    LIST_FIELDS = ["策略编号", "源表名称", "归档版本", "热数据天数", "冷存储路径", "授权分区", "到期时间", "归档格式", "压缩算法", "查询透传", "归档时间"]
    REQUIRED_FIELDS = ["策略编号", "源表名称", "热数据天数"]
    STATUS_ORDER = ["草稿", "待验证", "已生效", "已停用"]
    ACTION_RULES = {"提交验证": "待验证", "标记生效": "已生效", "停用策略": "已停用"}
    NEGATIVE_ACTIONS = ["停用策略"]

    PIPELINE = "cold_archive_pipeline"
    DOWNSTREAM_MODULES = ["retention"]
    PRODUCES_ARTIFACT = "冷归档操作记录"

    def execute_archive(self, entry_id: int) -> dict[str, Any]:
        """执行冷热分层归档。

        失败场景：
        - 冷存储路径不可达
        - 归档格式不兼容
        - 压缩算法不支持
        - 查询透传配置错误导致归档后不可查询
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "reason": f"策略 {entry_id} 不存在"}
        if entry.get("status") != "已生效":
            return {"ok": False, "reason": f"策略未生效，当前状态: {entry.get('status')}"}

        table_name = str(entry.get("源表名称", ""))
        hot_days = int(str(entry.get("热数据天数", "7")))
        cold_path = str(entry.get("冷存储路径", ""))
        archive_fmt = str(entry.get("归档格式", "parquet"))
        compression = str(entry.get("压缩算法", "zstd"))
        query_pass_through = str(entry.get("查询透传", "disabled"))

        if not cold_path:
            return {"ok": False, "reason": "冷存储路径为空，无法归档"}

        if archive_fmt not in ("parquet", "orc", "avro"):
            return {"ok": False, "reason": f"归档格式 {archive_fmt} 不支持"}

        if compression not in ("snappy", "gzip", "lz4", "zstd"):
            return {"ok": False, "reason": f"压缩算法 {compression} 不支持"}

        # 模拟归档操作
        archived_rows = 0
        table_rows = store.rows(table_name) if table_name in store._tables else []
        # 模拟：按热数据天数过滤
        cutoff_ts = time.time() - hot_days * 86400
        cold_rows = [r for r in table_rows if float(str(r.get("_ts", time.time()))) < cutoff_ts]
        archived_rows = len(cold_rows) or len(table_rows)  # 如果没有_ts则视为全部归档

        archive_version = str(entry.get("归档版本") or entry.get("archive_version") or f"AV-{entry_id}-{int(time.time())}")
        archive_record = {
            "策略编号": entry.get("策略编号"),
            "源表名称": table_name,
            "table_name": table_name,
            "归档版本": archive_version,
            "archive_version": archive_version,
            "冷存储路径": cold_path,
            "archive_path": cold_path,
            "归档格式": archive_fmt,
            "压缩算法": compression,
            "归档时间": time.strftime("%Y-%m-%d %H:%M:%S"),
            "archived_rows": archived_rows,
        }

        # 写入归档版本索引和清单（供 retention、版本授权和回填校验消费）
        store.add_archive_version(archive_record)

        entry["归档时间"] = time.strftime("%Y-%m-%d %H:%M:%S")

        batch_id = f"COLD_ARCHIVE_{entry_id}_{int(time.time())}"
        self.produce_artifact([archive_record], batch_id)

        return {
            "ok": True,
            "entry_id": entry_id,
            "table": table_name,
            "archive_version": archive_version,
            "归档版本": archive_version,
            "archived_rows": archived_rows,
            "cold_path": cold_path,
            "archive_fmt": archive_fmt,
        }

    def emit_archive_manifest(self) -> list[dict[str, Any]]:
        """向 retention 暴露归档清单"""
        return store.archive_manifest_for_table("") or store._archive_manifest

    # ---------- 归档版本授权：委托给统一授权领域服务 ----------
    def register_archive_version(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return archive_access.register_archive_version(*args, **kwargs)

    add_archive_version = register_archive_version
    create_archive_record = register_archive_version
    register_archive_record = register_archive_version

    def grant_archive_version(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return archive_access.create_grant(*args, **kwargs)

    grant_archive_access = grant_archive_version
    grant_archive_permission = grant_archive_version
    create_archive_grant = grant_archive_version
    create_archive_permission = grant_archive_version

    def revoke_archive_grant(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return archive_access.revoke_grant(*args, **kwargs)

    revoke_archive_access = revoke_archive_grant
    revoke_archive_permission = revoke_archive_grant
    revoke_permission = revoke_archive_grant

    def list_archive_grants(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return archive_access.list_grants(*args, **kwargs)

    def authorize_backfill(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return archive_access.authorize(*args, **kwargs)

    verify_archive_access = authorize_backfill
    check_archive_permission = authorize_backfill

    def write_archive_version(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return archive_access.assert_archive_write_allowed(*args, **kwargs)

    def run_action(
        self, entry_id: int, action: str, params: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any] | None, str]:
        params = dict(params or {})
        entry = store.find(self.MODULE, entry_id)
        action_map = {
            "新增归档版本授权": "grant",
            "新增版本授权": "grant",
            "归档版本授权": "grant",
            "授权归档版本": "grant",
            "grant_archive_version": "grant",
            "grant": "grant",
            "authorize": "grant",
            "撤销授权": "revoke",
            "撤回授权": "revoke",
            "撤销版本授权": "revoke",
            "revoke_archive_grant": "revoke",
            "revoke": "revoke",
        }
        kind = action_map.get(action)
        if kind == "grant":
            if entry is not None:
                params.setdefault("table_name", entry.get("源表名称"))
                params.setdefault("archive_path", entry.get("冷存储路径"))
                params.setdefault("archive_version", entry.get("归档版本") or entry.get("archive_version"))
            result = archive_access.create_grant(params)
            return result, result.get("message") or result.get("reason", "")
        if kind == "revoke":
            grant_id = params.get("grant_id") or params.get("id")
            if grant_id is None and entry is not None:
                grant_id = entry.get("授权编号") or entry.get("grant_id")
                params.setdefault("table_name", entry.get("源表名称"))
                params.setdefault("archive_version", entry.get("归档版本") or entry.get("archive_version"))
            result = archive_access.revoke_grant(grant_id, **params)
            return result, result.get("message") or result.get("reason", "")
        return super().run_action(entry_id, action, params)

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["archive_records"] = len(store._archive_manifest)
        return base