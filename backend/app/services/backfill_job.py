"""backfill_job 领域服务：历史数据回填任务。

业务逻辑：
1. 回填任务：基于快照基准向目标表回填历史数据
2. 回填链路：snapshot_mgr → backfill_job（使用快照作为数据基准）
3. 失败场景：依赖检查未通过、并行度超限、回填水位不连续
4. 与上下游流水线：消费 snapshot_mgr 快照；输出回填结果

跨模块数据流：
  snapshot_mgr → backfill_job（快照数据驱动历史回填）
"""
from __future__ import annotations

import time
from typing import Any

from app.services.archive_access import archive_access
from app.services.base import BaseModuleService
from app.store import store


class BackfillJobService(BaseModuleService):
    MODULE = "backfill_job"
    ENTITY = "回填任务"
    OPERATION_TERM = "回填"
    LIST_FIELDS = ["任务编号", "目标表", "归档版本", "请求分区", "窗口编号", "回填起始时间", "回填结束时间", "源表列表", "依赖检查", "并行度", "回填水位"]
    REQUIRED_FIELDS = ["任务编号", "目标表", "回填起始时间"]
    STATUS_ORDER = ["待执行", "运行中", "已完成", "已失败"]
    ACTION_RULES = {"发起回填": "运行中", "标记完成": "已完成", "标记失败": "已失败"}
    NEGATIVE_ACTIONS = []

    PIPELINE = "backfill_pipeline"
    UPSTREAM_MODULES = ["snapshot_mgr"]
    PRODUCES_ARTIFACT = "回填执行记录"

    def execute_backfill(self, entry_id: int, request: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        """执行回填：从快照基准向目标表写入历史数据。

        当请求或任务行带有 ``归档版本/archive_version`` 时，先执行表、精确版本、
        分区和授权有效期校验；普通快照回填仍保留原有行为。

        失败场景：
        - 依赖检查失败：上游表无快照
        - 并行度超限：线程数超过可用资源
        - 回填水位不连续：起始>结束或与已有数据冲突
        - 归档版本未授权、授权撤销/过期或分区越权
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "reason": f"任务 {entry_id} 不存在"}
        if entry.get("status") != "待执行":
            return {"ok": False, "reason": f"任务状态 {entry.get('status')} 非待执行"}

        request_context = {**(request or {}), **kwargs}
        context = {**entry, **request_context}
        archive_version = context.get("archive_version") or context.get("归档版本")
        if archive_version:
            auth = archive_access.authorize({
                **context,
                "table_name": context.get("table_name") or context.get("目标表"),
                "archive_version": archive_version,
                "partition": context.get("partition") or context.get("目标分区"),
                "action": "claim",
            })
            if not auth.get("ok"):
                entry["依赖检查"] = "归档授权失败: " + str(auth.get("reason"))
                entry["status"] = "已失败"
                entry["abnormal"] = True
                return auth

        target_table = str(request_context.get("目标表") or context.get("目标表", ""))
        source_tables = str(entry.get("源表列表", "")).split(",")
        parallelism = int(str(entry.get("并行度", "1")))

        # 依赖检查：验证源表有快照
        deps_ok = True
        dep_issues = []
        for st in source_tables:
            st = st.strip()
            if not st:
                continue
            has_snapshot = any(snap.get("table") == st for snap in store._snapshot_store.values())
            if not has_snapshot:
                dep_issues.append(f"源表 {st} 无可用快照")
                deps_ok = False
        if not deps_ok:
            entry["依赖检查"] = "失败: " + "; ".join(dep_issues)
            entry["status"] = "已失败"
            entry["abnormal"] = True
            return {"ok": False, "reason": "; ".join(dep_issues)}

        if parallelism > 10:
            return {"ok": False, "reason": f"并行度 {parallelism} 超限（最大10）"}

        # 模拟回填数据
        rows_to_insert = []
        for i in range(20):
            rows_to_insert.append({
                "table": target_table,
                "batch": entry.get("任务编号"),
                "row": i,
                "ts": time.time() - i * 3600,  # 历史时间
                "backfilled": True,
            })

        entry["依赖检查"] = "通过"
        entry["回填水位"] = str(time.time())
        entry["status"] = "运行中"
        entry["pending"] = True

        batch_id = f"BACKFILL_{entry_id}_{int(time.time())}"
        self.produce_artifact(rows_to_insert, batch_id)

        write_record: dict[str, Any] | None = None
        if archive_version and auth.get("ok"):
            claim = archive_access.claim_window({
                "task_id": entry.get("任务编号"),
                "entry_id": entry_id,
                "table_name": target_table,
                "archive_version": archive_version,
                "partition": context.get("partition") or context.get("目标分区") or context.get("请求分区"),
                "partitions": context.get("partitions"),
                "grant_id": auth.get("grant_id"),
                "row_count": len(rows_to_insert),
                "start": True,
            })
            if claim.get("ok"):
                confirmation = archive_access.confirm_segment(claim.get("window_id"))
                write_record = confirmation.get("write_record")

        return {
            "ok": True,
            "entry_id": entry_id,
            "target_table": target_table,
            "rows_inserted": len(rows_to_insert),
            "source_tables": source_tables,
            "dependencies_passed": len(source_tables),
            "archive_version": archive_version or None,
            "grant_id": auth.get("grant_id") if archive_version else None,
            "write_record": write_record,
        }

    # ---------- 归档版本授权驱动的窗口/分段流程 ----------
    def grant_archive_version(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return archive_access.create_grant(*args, **kwargs)

    grant_archive_access = grant_archive_version
    grant_archive_permission = grant_archive_version

    def revoke_archive_grant(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return archive_access.revoke_grant(*args, **kwargs)

    revoke_archive_access = revoke_archive_grant

    def authorize_archive_version(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return archive_access.authorize(*args, **kwargs)

    verify_archive_access = authorize_archive_version

    def accept_backfill_task(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return archive_access.accept_task(*args, **kwargs)

    accept_task = accept_backfill_task
    accept_backfill = accept_backfill_task

    def claim_window(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return archive_access.claim_window(*args, **kwargs)

    claim_backfill_window = claim_window
    claim_backfill_segment = claim_window
    claim_segment = claim_window
    acquire_segment = claim_window

    def start_segment(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return archive_access.start_segment(*args, **kwargs)

    start_backfill_segment = start_segment
    begin_segment = start_segment

    def confirm_segment(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return archive_access.confirm_segment(*args, **kwargs)

    confirm_backfill_segment = confirm_segment
    commit_segment = confirm_segment
    confirm_window = confirm_segment
    complete_segment = confirm_segment

    def withdraw_segment(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return archive_access.withdraw_window(*args, **kwargs)

    stop_segment = withdraw_segment

    def query_writes_by_version(self, *args: Any, **kwargs: Any) -> Any:
        return archive_access.query_writes_by_version(*args, **kwargs)

    def find_records_by_version(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return archive_access.find_records_by_version(*args, **kwargs)

    get_records_by_version = find_records_by_version
    query_records_by_version = find_records_by_version
    list_records_by_version = find_records_by_version
    get_writes_by_version = find_records_by_version
    find_writes_by_version = find_records_by_version

    def list_write_records(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return archive_access.list_write_records(*args, **kwargs)

    get_backfill_records = list_write_records

    def run_action(
        self, entry_id: int, action: str, params: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any] | None, str]:
        params = dict(params or {})
        entry = store.find(self.MODULE, entry_id)
        action_map = {
            "accept": "accept",
            "accept_task": "accept",
            "接受授权任务": "accept",
            "接受回填任务": "accept",
            "accept_backfill_task": "accept",
            "claim": "claim",
            "claim_window": "claim",
            "领取窗口": "claim",
            "领取分段": "claim",
            "start": "start",
            "start_segment": "start",
            "开始分段": "start",
            "confirm": "confirm",
            "confirm_segment": "confirm",
            "确认分段": "confirm",
            "确认窗口": "confirm",
            "withdraw": "withdraw",
            "withdraw_segment": "withdraw",
            "撤回分段": "withdraw",
        }
        kind = action_map.get(action)
        if kind:
            domain_params: dict[str, Any] = {}
            if entry is not None:
                domain_params.update({key: value for key, value in entry.items() if key != "id"})
                domain_params.update({
                    "entry_id": entry_id,
                    "task_pk": entry_id,
                    "task_id": entry.get("任务编号"),
                    "target_table": entry.get("目标表"),
                    "table_name": entry.get("目标表"),
                })
                if entry.get("窗口编号"):
                    domain_params["window_id"] = entry.get("窗口编号")
                if entry.get("归档版本"):
                    domain_params["archive_version"] = entry.get("归档版本")
            domain_params.update(params)
            method_name = {
                "accept": "accept_task",
                "claim": "claim_window",
                "start": "start_segment",
                "confirm": "confirm_segment",
                "withdraw": "withdraw_window",
            }[kind]
            result = getattr(archive_access, method_name)(payload=domain_params)
            return result, str(result.get("message") or result.get("reason") or action)
        return super().run_action(entry_id, action, params)

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["snapshot_dependencies"] = len(store._snapshot_store)
        return base