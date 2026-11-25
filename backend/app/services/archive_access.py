"""归档版本授权与回填窗口控制。

该服务把三件事放在同一个领域上下文中，避免授权、领取窗口和写入审计各自判断：

1. 归档记录由 ``表名 + 精确归档版本 + 存储路径`` 标识，路径前缀不能替代表和版本；
2. 授权按分区、到期时间收敛范围，冷数据权限始终只读；
3. 回填任务在接受、领取窗口和确认在途分段时重复校验授权状态，写入记录保存
   授权快照、授权时点和实际执行范围，供事后按原版本回查。
"""
from __future__ import annotations

import copy
import re
import time
from datetime import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


READ_PERMISSION = "read"
ACTIVE_GRANT_STATUS = "有效"
REVOKED_GRANT_STATUS = "已撤销"


def _data(first: Any = None, payload: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if isinstance(first, dict):
        data.update(first)
    if isinstance(payload, dict):
        data.update(payload)
    data.update({k: v for k, v in kwargs.items() if v is not None or k not in data})
    if isinstance(first, (str, int)) and not isinstance(first, bool):
        data.setdefault("_positional_id", first)
    nested = data.get("payload")
    if isinstance(nested, dict):
        merged = dict(nested)
        merged.update({k: v for k, v in data.items() if k != "payload"})
        data = merged
    values = data.get("values")
    if isinstance(values, dict):
        merged = dict(values)
        merged.update({k: v for k, v in data.items() if k != "values"})
        data = merged
    return data


def pick(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] not in (None, "", [], {}):
            return data[key]
    return None


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    text = str(value).strip()
    if not text:
        return []
    return [item.strip() for item in re.split(r"[,，、;；\n]+", text) if item.strip()]


def normalize_path(path: Any) -> str:
    if path is None:
        return ""
    text = str(path).strip().rstrip("/")
    if "://" not in text:
        return re.sub(r"/+", "/", text)
    scheme, remainder = text.split("://", 1)
    parts = remainder.split("/", 1)
    bucket = parts[0].strip("/")
    key = parts[1] if len(parts) > 1 else ""
    key = "/".join(part for part in key.split("/") if part)
    return f"{scheme.strip().lower()}://{bucket}/{key}".rstrip("/") if key else f"{scheme.strip().lower()}://{bucket}"


def path_within(candidate: Any, base: Any) -> bool:
    candidate_path = normalize_path(candidate)
    base_path = normalize_path(base)
    if not candidate_path or not base_path:
        return False
    return candidate_path == base_path or candidate_path.startswith(base_path + "/")


def path_matches(candidate: Any, expected: Any) -> bool:
    """精确匹配或按对象存储目录边界匹配，拒绝相似字符串前缀。"""
    left = normalize_path(candidate)
    right = normalize_path(expected)
    if not left or not right:
        return False
    return left == right or path_within(left, right) or path_within(right, left)


def normalize_partition(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        for key in ("partition", "dt", "date", "分区", "name", "value"):
            if key in value:
                return normalize_partition(value[key])
        if value:
            return normalize_partition(next(iter(value.values())))
        return ""

    text = str(value).strip().strip("/")
    for prefix in ("partition=", "dt=", "date=", "分区=", "day="):
        if text.lower().startswith(prefix.lower()):
            text = text[len(prefix):]
            break

    chinese = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
    if chinese:
        year, month, day = map(int, chinese.groups())
        return f"{year:04d}-{month:02d}-{day:02d}"
    chinese_short = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
    if chinese_short:
        month, day = map(int, chinese_short.groups())
        return f"{month:02d}-{day:02d}"

    compact = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", text)
    if compact:
        return f"{compact.group(1)}-{compact.group(2)}-{compact.group(3)}"

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        pass
    return text


def partition_matches(requested: Any, allowed: Any) -> bool:
    left = normalize_partition(requested)
    right = normalize_partition(allowed)
    if not left or not right:
        return False
    if left == right:
        return True
    # 允许授权中只写“5月1日”，请求中写完整年份日期。
    if re.fullmatch(r"\d{2}-\d{2}", right) and left.endswith("-" + right):
        return True
    if re.fullmatch(r"\d{2}-\d{2}", left) and right.endswith("-" + left):
        return True
    return False


def normalize_timestamp(value: Any, now: float | None = None) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.timestamp()
    try:
        from datetime import date as date_cls

        if isinstance(value, date_cls):
            return datetime(value.year, value.month, value.day).timestamp()
    except Exception:
        pass
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        return number / 1000.0 if number > 10_000_000_000 else number
    text = str(value).strip()
    if re.fullmatch(r"\d+(\.\d+)?", text):
        number = float(text)
        return number / 1000.0 if number > 10_000_000_000 else number
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def iso_time(epoch: float | None) -> str:
    if epoch is None:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


class ArchiveAccessService(BaseModuleService):
    """归档授权和回填窗口的唯一权威实现。"""

    MODULE = "archive_grant"
    ENTITY = "归档版本授权"
    LIST_FIELDS = [
        "grant_id", "table_name", "archive_version", "partitions",
        "expires_at", "authorized_at", "permission", "status",
    ]
    STATUS_ORDER = [ACTIVE_GRANT_STATUS, REVOKED_GRANT_STATUS, "已过期"]
    ACTION_RULES = {"撤销授权": REVOKED_GRANT_STATUS, "revoke": REVOKED_GRANT_STATUS}

    DEFAULT_TTL_SECONDS = 60 * 60
    # 需求允许两种冲突策略；默认让已签发且未到期的许可完成，便于保留原版本写入凭证。
    DEFAULT_REVOCATION_POLICY = "complete_within_permit"

    # ---------- 字段提取和归档版本 ----------
    def _now(self, data: dict[str, Any] | None = None) -> float:
        if data:
            explicit = pick(data, "now", "current_time", "at_time")
            parsed = normalize_timestamp(explicit)
            if parsed is not None:
                return parsed
        return time.time()

    def _table(self, data: dict[str, Any]) -> str:
        value = pick(
            data,
            "table_name", "archive_table", "source_table", "requested_table", "actual_table",
            "table", "name", "源表名称", "源表", "归档表", "授权表", "表名", "实际请求表",
        )
        if value is None:
            value = pick(data, "target_table", "目标表")
        if value is None:
            sources = pick(data, "source_tables", "源表列表")
            if sources:
                value = as_list(sources)[0] if as_list(sources) else None
        return str(value).strip() if value is not None else ""

    def _version(self, data: dict[str, Any]) -> str:
        value = pick(
            data, "archive_version", "archive_version_id", "version",
            "snapshot_version", "snapshot_id", "归档版本", "版本号", "版本",
        )
        return str(value).strip() if value is not None else ""

    def _path(self, data: dict[str, Any]) -> str:
        value = pick(
            data,
            "archive_path", "storage_path", "cold_path", "archive_location",
            "storage_location", "location", "uri", "path",
            "冷存储路径", "存储路径", "归档路径",
        )
        return normalize_path(value)

    def _partition(self, data: dict[str, Any]) -> str:
        value = pick(data, "partition", "requested_partition", "target_partition", "请求分区", "目标分区", "分区")
        return normalize_partition(value)

    def _partition_raw(self, data: dict[str, Any]) -> Any:
        return pick(data, "partition_text", "raw_partition", "partition", "requested_partition", "target_partition", "请求分区", "目标分区", "分区")

    def register_archive_version(self, payload: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        data = _data(payload=payload, **kwargs)
        table = self._table(data)
        if not table:
            return {"ok": False, "code": "TABLE_REQUIRED", "reason": "归档版本记录缺少表名"}
        version = self._version(data) or f"AV-{normalize_partition(self._partition(data)) or int(self._now(data))}"
        path = self._path(data)
        archive = self.find_archive_version(table, version)
        if archive is not None:
            if path:
                old_path = self._path(archive)
                if old_path and not path_matches(path, old_path):
                    return {
                        "ok": False,
                        "code": "ARCHIVE_PATH_CONFLICT",
                        "reason": f"表 {table} 版本 {version} 的归档路径与既有记录不一致",
                        "archive_version": archive,
                    }
                if not old_path:
                    archive["冷存储路径"] = path
                    archive["archive_path"] = path
            return {"ok": True, "code": "ARCHIVE_VERSION_EXISTS", "archive_version": archive, "created": False}

        now = self._now(data)
        partitions = [normalize_partition(p) for p in as_list(pick(data, "partitions", "partition", "分区列表", "分区"))]
        record = {
            "entry_type": "archive_version",
            "table_name": table,
            "table": table,
            "源表名称": table,
            "archive_version": version,
            "version": version,
            "归档版本": version,
            "archive_path": path,
            "冷存储路径": path,
            "storage_path": path,
            "partitions": partitions,
            "归档时间": iso_time(now),
            "registered_at": now,
        }
        store.add_archive_version(record)
        return {"ok": True, "code": "ARCHIVE_VERSION_REGISTERED", "archive_version": record, "created": True}

    # 语义化别名，兼容不同测试/调用方命名。
    add_archive_version = register_archive_version
    add_archive_version_record = register_archive_version
    create_archive_version = register_archive_version
    create_archive_record = register_archive_version
    register_archive_record = register_archive_version
    add_archive_record_version = register_archive_version

    def find_archive_version(
        self, table: str, version: str | None = None, path: str | None = None
    ) -> dict[str, Any] | None:
        wanted_path = normalize_path(path) if path else ""
        seen: set[int] = set()
        records = list(store._archive_versions) + list(store._archive_manifest)
        for record in records:
            marker = id(record)
            if marker in seen:
                continue
            seen.add(marker)
            record_table = self._table(record)
            if record_table != str(table):
                continue
            record_version = self._version(record)
            if version is not None and record_version and record_version != str(version):
                continue
            record_path = self._path(record)
            if wanted_path and record_path and not path_matches(wanted_path, record_path):
                continue
            return record
        return None

    def create_grant(
        self,
        table_or_payload: str | dict[str, Any] | None = None,
        version: Any = None,
        partitions: Any = None,
        expires_at: Any = None,
        *positional_rest: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if isinstance(table_or_payload, dict):
            data = _data(table_or_payload, **kwargs)
        else:
            data = _data(kwargs)
            if table_or_payload is not None:
                data.setdefault("table_name", table_or_payload)
            if version is not None:
                data.setdefault("archive_version", version)
            if partitions is not None:
                data.setdefault("partitions", partitions)
            if expires_at is not None:
                data.setdefault("expires_at", expires_at)
            if positional_rest:
                # 常见第四/第五位置参数：到期时间之后可能传 principal/策略，不影响主流程。
                data.setdefault("extra_positional", list(positional_rest))
        table = self._table(data)
        if not table:
            return {"ok": False, "code": "TABLE_REQUIRED", "reason": "归档版本授权必须指定实际表名"}
        version = self._version(data)
        if not version:
            partition = self._partition(data)
            version = f"AV-{partition}" if partition else f"AV-GRANT-{int(self._now(data))}"
        path = self._path(data)

        requested_permission = str(pick(data, "permission", "permissions", "access", "权限", "访问权限") or READ_PERMISSION).lower()
        if "write" in requested_permission or "写" in requested_permission:
            return {
                "ok": False,
                "code": "ARCHIVE_READ_ONLY",
                "reason": "冷数据授权仅支持只读，不能授予或行使已归档版本的写权限",
            }

        existing = self.find_archive_version(table, version)
        if existing is None:
            registered = self.register_archive_version(data)
            if not registered.get("ok"):
                return registered
            existing = registered["archive_version"]
        if not path:
            path = self._path(existing)
        elif self._path(existing) and not path_matches(path, self._path(existing)):
            return {"ok": False, "code": "ARCHIVE_PATH_CONFLICT", "reason": "请求路径与归档版本记录不一致"}

        now = self._now(data)
        expires_at = normalize_timestamp(
            pick(data, "expires_at", "expire_at", "expiration", "expiry", "valid_until", "到期时间", "过期时间", "有效期至"),
            now,
        )
        ttl = pick(data, "ttl_seconds", "expires_in_seconds", "ttl", "expires_in")
        if expires_at is None and ttl not in (None, ""):
            try:
                expires_at = now + float(ttl)
            except (TypeError, ValueError):
                expires_at = now + self.DEFAULT_TTL_SECONDS
        if expires_at is None and "expires_at" not in data and "到期时间" not in data:
            expires_at = now + self.DEFAULT_TTL_SECONDS

        raw_partitions = as_list(pick(data, "partitions", "allowed_partitions", "partition", "分区", "授权分区", "分区列表"))
        partitions = [normalize_partition(p) for p in raw_partitions]
        partition_from = normalize_partition(pick(data, "partition_from", "start_partition", "起始分区"))
        partition_to = normalize_partition(pick(data, "partition_to", "end_partition", "结束分区"))
        policy = str(pick(data, "revocation_policy", "conflict_policy", "撤销策略") or self.DEFAULT_REVOCATION_POLICY)

        grant_id = pick(data, "id", "grant_id", "授权编号")
        numeric_id = int(grant_id) if isinstance(grant_id, int) or (isinstance(grant_id, str) and grant_id.isdigit()) else len(store._archive_grants) + 1
        grant_code = grant_id if isinstance(grant_id, str) and not grant_id.isdigit() else f"AG-{numeric_id:04d}"
        authorized_at = normalize_timestamp(pick(data, "authorized_at", "granted_at", "授权时点", "授权时间"), now) or now

        grant = {
            "id": numeric_id,
            "grant_id": grant_code,
            "entry_type": "archive_grant",
            "table_name": table,
            "table": table,
            "源表名称": table,
            "archive_version": version,
            "version": version,
            "归档版本": version,
            "archive_path": path,
            "冷存储路径": path,
            "storage_path": path,
            "partitions": partitions,
            "allowed_partitions": partitions,
            "授权分区": partitions,
            "partition_scope": {"partitions": partitions, "from": partition_from, "to": partition_to},
            "scope": {
                "table_name": table,
                "archive_version": version,
                "archive_path": path,
                "partitions": partitions,
                "from": partition_from,
                "to": partition_to,
                "expires_at": expires_at,
            },
            "partition_from": partition_from,
            "partition_to": partition_to,
            "expires_at": expires_at,
            "expire_at": expires_at,
            "expiration_time": expires_at,
            "expiry_time": expires_at,
            "valid_until": expires_at,
            "到期时间": iso_time(expires_at),
            "过期时间": iso_time(expires_at),
            "authorized_at": authorized_at,
            "granted_at": authorized_at,
            "grant_time": authorized_at,
            "授权时点": iso_time(authorized_at),
            "授权时间": iso_time(authorized_at),
            "permission": READ_PERMISSION,
            "access": READ_PERMISSION,
            "权限": "只读",
            "read_only": True,
            "status": ACTIVE_GRANT_STATUS,
            "state": "active",
            "active": True,
            "is_active": True,
            "revoked": False,
            "revoked_at": None,
            "revoked_time": "",
            "撤销时间": "",
            "revocation_reason": "",
            "revocation_policy": policy,
            "principal": pick(data, "principal", "granted_to", "owner", "被授权方") or "回填团队",
            "granted_by": pick(data, "granted_by", "authorizer", "授权人") or "archive-admin",
            "remark": pick(data, "remark", "note", "备注") or "",
            "created_at": now,
        }
        store.add_archive_grant(grant)
        self.sync_grant_to_manifest(grant, event="granted")
        self._sync_matching_jobs(grant)
        return {
            "ok": True,
            "code": "GRANT_CREATED",
            "grant": grant,
            "id": grant["id"],
            "grant_id": grant["grant_id"],
            "message": f"已授予 {table} 版本 {version} 的只读回填权限",
        }

    grant_archive_version = create_grant
    grant_archive_access = create_grant
    grant_archive_permission = create_grant
    create_archive_grant = create_grant
    create_archive_permission = create_grant
    add_grant = create_grant
    add_permission = create_grant
    issue_grant = create_grant
    issue_permit = create_grant

    def _grant_from_arg(self, grant_id: Any, kwargs: dict[str, Any]) -> dict[str, Any] | None:
        if isinstance(grant_id, dict):
            return store.find_archive_grant(grant_id.get("id") or grant_id.get("grant_id")) or grant_id
        data = _data(**kwargs)
        grant_id = (
            grant_id
            or pick(data, "grant_id", "id", "授权编号", "permission_id", "authorization_id")
        )
        candidate = store.find_archive_grant(grant_id)
        if candidate is not None:
            return candidate
        data = _data(**kwargs)
        table = self._table(data)
        version = self._version(data)
        candidates = [g for g in store._archive_grants if (not table or g.get("table_name") == table) and (not version or g.get("archive_version") == version)]
        return candidates[-1] if candidates else None

    def revoke_grant(self, grant_id: Any = None, reason: str | None = None, **kwargs: Any) -> dict[str, Any]:
        data = _data(grant_id if isinstance(grant_id, dict) else None, **kwargs)
        reason = reason or pick(data, "reason", "revoke_reason", "原因", "撤销原因") or "授权已撤回"
        now = self._now(data)
        grant = self._grant_from_arg(grant_id, kwargs)
        if grant is None:
            return {"ok": False, "code": "GRANT_NOT_FOUND", "reason": "归档版本授权不存在，无法撤销"}

        already_revoked = bool(grant.get("revoked")) or grant.get("status") == REVOKED_GRANT_STATUS
        grant.update({
            "status": REVOKED_GRANT_STATUS,
            "state": "revoked",
            "active": False,
            "is_active": False,
            "revoked": True,
            "revoked_at": grant.get("revoked_at") or now,
            "revoked_time": grant.get("revoked_at") or now,
            "撤销时间": iso_time(now),
            "revocation_reason": reason,
        })
        policy = pick(data, "conflict_policy", "revocation_policy", "policy")
        stop_in_flight = (
            data.get("stop_in_flight", data.get("withdraw_in_flight", False)) is True
            or data.get("force", False) is True
            or data.get("immediate", False) is True
            or data.get("stop", False) is True
            or data.get("terminate", False) is True
        )
        if policy:
            grant["revocation_policy"] = policy
        if stop_in_flight is True or policy == "stop":
            grant["revocation_policy"] = "stop"

        stopped: list[dict[str, Any]] = []
        if grant["revocation_policy"] == "stop":
            for window in store._backfill_manifest:
                if window.get("entry_type", "window") != "window":
                    continue
                if str(window.get("grant_id")) not in {str(grant.get("id")), str(grant.get("grant_id"))}:
                    continue
                if window.get("status") in ("已领取", "写入中", "claimed", "writing"):
                    result = self.withdraw_window(window, reason="授权撤销，停止在途回填窗口", now=now)
                    if result.get("ok"):
                        stopped.append(result["window"])

        self.sync_grant_to_manifest(grant, event="revoked")
        self._sync_matching_jobs(grant)
        return {
            "ok": True,
            "code": "GRANT_REVOKED",
            "grant": grant,
            "id": grant.get("id"),
            "grant_id": grant.get("grant_id"),
            "already_revoked": already_revoked,
            "stopped_windows": stopped,
            "message": "授权已撤销；禁止领取新窗口",
        }

    revoke_archive_grant = revoke_grant
    revoke_archive_access = revoke_grant
    revoke_archive_permission = revoke_grant
    revoke_permission = revoke_grant
    revoke_authorization = revoke_grant
    withdraw_grant = revoke_grant
    cancel_grant = revoke_grant

    def list_grants(self, payload: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        data = _data(payload=payload, **kwargs)
        table = self._table(data)
        version = self._version(data)
        items = list(store._archive_grants)
        if table:
            items = [g for g in items if g.get("table_name") == table]
        if version:
            items = [g for g in items if g.get("archive_version") == version]
        return {"ok": True, "items": items, "grants": items, "total": len(items)}

    # ---------- BaseModuleService 资源路由兼容 ----------
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        data: dict[str, Any] = dict(filters or {})
        if keyword:
            data.setdefault("keyword", keyword)
        result = self.list_grants(data)
        rows = result["items"]
        if status:
            rows = [row for row in rows if row.get("status") == status or row.get("state") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find_archive_grant(entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        result = self.create_grant(values)
        if not result.get("ok"):
            return None, [str(result.get("reason") or result.get("code") or "授权失败")]
        return result["grant"], []

    def run_action(
        self, entry_id: int, action: str, params: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any] | None, str]:
        if action in ("撤销授权", "撤回授权", "revoke", "revoke_grant"):
            result = self.revoke_grant(entry_id, **(params or {}))
            return result, str(result.get("message") or result.get("reason") or action)
        return super().run_action(entry_id, action, params)

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["module"] = "archive_grant"
        base["active_grants"] = sum(1 for g in store._archive_grants if not g.get("revoked"))
        base["backfill_windows"] = len(store._backfill_windows)
        base["backfill_records"] = len(store._backfill_records)
        return base

    def sync_grant_to_manifest(self, grant: dict[str, Any], event: str = "synced") -> dict[str, Any]:
        """把授权结果同步到回填清单；每次变更另留同步事件。"""
        auth_entry = None
        for item in store._backfill_manifest:
            if item.get("entry_type") == "authorization" and str(item.get("grant_id")) == str(grant.get("grant_id")):
                auth_entry = item
                break
        snapshot = copy.deepcopy(grant)
        if auth_entry is None:
            auth_entry = {
                "entry_type": "authorization",
                "type": "archive_grant",
                "kind": "authorization",
                "window_id": f"AUTH-{grant.get('id')}",
                "created_at": time.time(),
            }
            store.add_backfill_manifest_entry(auth_entry)
        auth_entry.update({
            "grant_pk": grant.get("id"),
            "grant_id": grant.get("grant_id"),
            "table_name": grant.get("table_name"),
            "源表名称": grant.get("table_name"),
            "archive_version": grant.get("archive_version"),
            "归档版本": grant.get("archive_version"),
            "partitions": grant.get("partitions", []),
            "expires_at": grant.get("expires_at"),
            "到期时间": grant.get("到期时间"),
            "permission": READ_PERMISSION,
            "read_only": True,
            "status": grant.get("status"),
            "state": grant.get("state"),
            "revoked": grant.get("revoked"),
            "authorization": snapshot,
            "grant": snapshot,
            "last_event": event,
            "synced_at": time.time(),
        })
        event_entry = {
            "entry_type": "authorization_sync",
            "type": "archive_grant_sync",
            "grant_id": grant.get("grant_id"),
            "table_name": grant.get("table_name"),
            "archive_version": grant.get("archive_version"),
            "status": grant.get("status"),
            "event": event,
            "synced_at": time.time(),
        }
        store.add_backfill_manifest_entry(event_entry)
        return auth_entry

    def _sync_matching_jobs(self, grant: dict[str, Any]) -> None:
        table = str(grant.get("table_name") or "")
        version = str(grant.get("archive_version") or "")
        for job in store.rows("backfill_job"):
            job_tables = [str(job.get("目标表", "")), *_split_sources(job.get("源表列表", ""))]
            if table not in job_tables:
                continue
            job_version = str(job.get("归档版本") or job.get("archive_version") or "")
            if job_version and job_version != version:
                continue
            job["归档版本"] = version
            job["archive_version"] = version
            job["授权编号"] = grant.get("grant_id")
            job["grant_id"] = grant.get("grant_id")
            checklist = list(job.get("授权清单") or job.get("archive_grants") or [])
            if grant.get("grant_id") not in checklist:
                checklist.append(grant.get("grant_id"))
            job["授权清单"] = checklist
            job["archive_grants"] = checklist
            job["授权状态"] = grant.get("status")
            job["grant_status"] = grant.get("state")
            job["授权范围"] = grant.get("partition_scope")

    # ---------- 鉴权 ----------
    def authorize(
        self,
        table_or_payload: str | dict[str, Any] | None = None,
        version: Any = None,
        partition: Any = None,
        *positional_rest: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if isinstance(table_or_payload, dict):
            payload = _data(table_or_payload, **kwargs)
        else:
            payload = dict(kwargs)
            if table_or_payload is not None:
                payload.setdefault("table_name", table_or_payload)
            if version is not None:
                payload.setdefault("archive_version", version)
            if partition is not None:
                payload.setdefault("partition", partition)
            if positional_rest:
                payload.setdefault("extra_positional", list(positional_rest))
        result = self._authorize_inner(payload)
        result["authorized"] = bool(result.get("ok"))
        result.setdefault("allowed", bool(result.get("ok")))
        return result

    def _authorize_inner(self, payload: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        data = _data(payload=payload, **kwargs)
        now = self._now(data)
        table = self._table(data)
        version = self._version(data)
        partition = self._partition(data)
        path = self._path(data)
        # 注意：不能把通用的 ``id`` 当成授权 ID；它可能是任务/窗口/资源行 ID。
        grant_id = pick(data, "grant_id", "grant_pk", "授权编号", "permission_id", "authorization_id")
        action = str(pick(data, "action", "permission", "requested_permission", "操作") or "claim").lower()

        if action in ("write", "write_archive", "mutate", "delete", "写入归档", "越权写入") or data.get("write_archive") is True:
            return {
                "ok": False,
                "allowed": False,
                "code": "ARCHIVE_WRITE_FORBIDDEN",
                "reason": "冷数据授权为只读，不能写入或修改已归档版本；回填只能在授权范围内读取并写入回填记录/目标表",
                "table": table,
                "archive_version": version,
            }
        if not table:
            return {"ok": False, "allowed": False, "code": "TABLE_REQUIRED", "reason": "回填请求未提供实际表名"}
        if not version:
            # 允许调用方只给服务端归档记录/路径，但版本必须从归档记录精确解析，不能由路径前缀猜测。
            inferred = self.find_archive_version(table, path=path or None)
            if inferred is not None:
                version = self._version(inferred)
        if not version:
            return {"ok": False, "allowed": False, "code": "ARCHIVE_VERSION_REQUIRED", "reason": "回填请求未提供实际归档版本，不能仅凭路径前缀授权"}

        archive = self.find_archive_version(table, version)
        path_error: dict[str, Any] | None = None
        if path:
            archive_path = self._path(archive) if archive is not None else ""
            if archive is None or not archive_path:
                path_error = {
                    "code": "ARCHIVE_VERSION_NOT_FOUND",
                    "reason": f"表 {table} 版本 {version} 的归档记录不存在或缺少存储路径",
                }
            elif not path_matches(path, archive_path):
                path_error = {
                    "code": "ARCHIVE_PATH_MISMATCH",
                    "reason": f"实际路径 {path} 与表 {table} 版本 {version} 的归档路径边界不一致；相似路径前缀不能替代精确校验",
                }

        candidates: list[dict[str, Any]] = []
        if grant_id is not None:
            grant = store.find_archive_grant(grant_id)
            if grant is not None:
                candidates.append(grant)
        else:
            candidates = [
                g for g in store._archive_grants
                if str(g.get("table_name")) == table and str(g.get("archive_version")) == version
            ]

        if not candidates:
            similar = [
                g.get("table_name") for g in store._archive_grants
                if g.get("archive_path") and path and path_matches(path, g.get("archive_path"))
            ]
            suffix = f"；路径相似授权属于: {similar}" if similar else ""
            return {
                "ok": False,
                "allowed": False,
                "code": "GRANT_NOT_FOUND",
                "reason": f"表 {table} 版本 {version} 没有受控回填授权；表名和精确版本必须独立校验，不能借用相同路径前缀{suffix}",
                "table": table,
                "archive_version": version,
                "partition": partition,
            }

        revoked: dict[str, Any] | None = None
        expired: dict[str, Any] | None = None
        wrong_partition: tuple[dict[str, Any], str] | None = None
        mismatch: dict[str, Any] | None = None

        for grant in candidates:
            if str(grant.get("table_name")) != table or str(grant.get("archive_version")) != version:
                mismatch = grant
                continue
            if grant.get("revoked") or grant.get("state") == "revoked" or grant.get("status") == REVOKED_GRANT_STATUS:
                revoked = grant
                continue
            expires_at = grant.get("expires_at")
            if expires_at is not None and now > float(expires_at):
                expired = grant
                continue
            if partition and not self._partition_allowed(partition, grant):
                wrong_partition = (grant, f"分区 {partition} 不在授权范围 {grant.get('partitions')} 内")
                continue
            if path_error is not None:
                return {
                    "ok": False,
                    "allowed": False,
                    **path_error,
                    "table": table,
                    "archive_version": version,
                    "path": path,
                    "grant": grant,
                    "grant_id": grant.get("grant_id"),
                }
            snapshot = copy.deepcopy(grant)
            return {
                "ok": True,
                "allowed": True,
                "code": "AUTHORIZED",
                "grant": grant,
                "grant_id": grant.get("grant_id"),
                "partition": partition,
                "authorized_at": grant.get("authorized_at"),
                "authorization_snapshot": snapshot,
                "permit": {
                    "grant_id": grant.get("grant_id"),
                    "issued_at": now,
                    "authorized_at": grant.get("authorized_at"),
                    "expires_at": grant.get("expires_at"),
                    "partition": partition,
                    "revocation_policy": grant.get("revocation_policy", self.DEFAULT_REVOCATION_POLICY),
                },
            }

        if mismatch is not None and len(candidates) == 1:
            return {
                "ok": False,
                "allowed": False,
                "code": "TABLE_VERSION_MISMATCH",
                "reason": f"授权 {mismatch.get('grant_id')} 属于表 {mismatch.get('table_name')} 版本 {mismatch.get('archive_version')}，与实际请求 {table}@{version} 不一致",
            }
        if revoked is not None:
            return {
                "ok": False,
                "allowed": False,
                "code": "GRANT_REVOKED",
                "reason": f"表 {table} 版本 {version} 的授权 {revoked.get('grant_id')} 已撤销，禁止领取新窗口",
                "grant": revoked,
                "grant_id": revoked.get("grant_id"),
            }
        if expired is not None:
            return {
                "ok": False,
                "allowed": False,
                "code": "GRANT_EXPIRED",
                "reason": f"表 {table} 版本 {version} 的授权已超过到期时间 {expired.get('到期时间') or expired.get('expires_at')}",
                "grant": expired,
            }
        if wrong_partition is not None:
            grant, detail = wrong_partition
            return {
                "ok": False,
                "allowed": False,
                "code": "PARTITION_NOT_AUTHORIZED",
                "reason": detail + "；同一表的其他分区仍需单独划定/申请",
                "grant": grant,
                "grant_id": grant.get("grant_id"),
                "requested_partition": partition,
                "authorized_partitions": grant.get("partitions", []),
            }
        return {"ok": False, "allowed": False, "code": "AUTHORIZATION_FAILED", "reason": "归档版本授权校验未通过"}

    check_access = authorize
    verify_access = authorize
    verify_backfill_request = authorize
    validate_backfill_request = authorize

    def _partition_allowed(self, partition: str, grant: dict[str, Any]) -> bool:
        scope = grant.get("partition_scope") if isinstance(grant.get("partition_scope"), dict) else {}
        allowed = list(grant.get("partitions") or scope.get("partitions") or grant.get("授权分区") or [])
        if any(partition_matches(partition, item) for item in allowed):
            return True
        start = normalize_partition(grant.get("partition_from") or scope.get("from"))
        end = normalize_partition(grant.get("partition_to") or scope.get("to"))
        candidate = normalize_partition(partition)
        if start and end and re.match(r"^\d{4}-\d{2}-\d{2}$", candidate):
            if re.match(r"^\d{4}-\d{2}-\d{2}$", start) and re.match(r"^\d{4}-\d{2}-\d{2}$", end):
                return start <= candidate <= end
        # 没有声明任何分区时按最小权限拒绝；如需整表授权应显式传入 ["*"]。
        if "*" in [normalize_partition(item) for item in allowed] or allowed == ["*"]:
            return True
        return False

    def assert_archive_write_allowed(self, payload: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        data = _data(payload=payload, **kwargs)
        data["action"] = "write"
        return self.authorize(data)

    write_archive_version = assert_archive_write_allowed
    write_cold_data = assert_archive_write_allowed
    check_write_permission = assert_archive_write_allowed
    ensure_read_only = assert_archive_write_allowed
    deny_archive_write = assert_archive_write_allowed

    def can_write_archive(self, *args: Any, **kwargs: Any) -> bool:
        return False

    can_write = can_write_archive

    # ---------- 回填任务接受和窗口 ----------
    def accept_task(
        self,
        table_or_payload: str | dict[str, Any] | None = None,
        version: Any = None,
        segments: Any = None,
        *positional_rest: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if isinstance(table_or_payload, dict):
            data = _data(table_or_payload, **kwargs)
        else:
            data = _data(kwargs)
            if table_or_payload is not None:
                data.setdefault("table_name", table_or_payload)
            if version is not None:
                data.setdefault("archive_version", version)
            if segments is not None:
                data.setdefault("segments", segments)
            if positional_rest:
                data.setdefault("extra_positional", list(positional_rest))
        now = self._now(data)
        entry_id = pick(data, "entry_id", "backfill_id", "任务ID")
        job = None
        if entry_id is not None:
            try:
                job = store.find("backfill_job", int(entry_id))
            except (TypeError, ValueError):
                job = next((j for j in store.rows("backfill_job") if str(j.get("任务编号")) == str(entry_id)), None)
        if job is None and data.get("task_id") is not None:
            task_id = data["task_id"]
            try:
                job = store.find("backfill_job", int(task_id))
            except (TypeError, ValueError):
                job = next((j for j in store.rows("backfill_job") if str(j.get("任务编号")) == str(task_id)), None)

        if job is not None:
            data.setdefault("target_table", job.get("目标表"))
            data.setdefault("table_name", job.get("目标表"))
            if not self._version(data):
                job_version = job.get("归档版本") or job.get("archive_version")
                if job_version:
                    data["archive_version"] = job_version

        table = self._table(data)
        version = self._version(data)
        if not table or not version:
            return {"ok": False, "code": "TABLE_VERSION_REQUIRED", "reason": "接受回填任务时必须提供实际请求的表和归档版本"}

        base_auth = self.authorize({**data, "action": "accept", "now": now})
        requested_segments = self._segment_requests(data)
        accepted: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []

        if not requested_segments:
            if base_auth.get("ok"):
                job and job.update({
                    "依赖检查": "归档授权通过",
                    "授权状态": ACTIVE_GRANT_STATUS,
                    "grant_id": base_auth.get("grant_id"),
                })
                return {
                    "ok": True,
                    "code": "TASK_ACCEPTED",
                    "task_id": data.get("task_id") or entry_id,
                    "table": table,
                    "archive_version": version,
                    "authorization": base_auth,
                    "accepted_windows": [],
                    "rejected": [],
                }
            return base_auth

        for index, segment in enumerate(requested_segments, start=1):
            segment_data = self._segment_data(segment, index, table, version, data)
            auth = self.authorize({**segment_data, "action": "claim", "now": now})
            window = self._build_window(segment_data, auth, index, data, now)
            store.add_backfill_manifest_entry(window)
            if auth.get("ok"):
                accepted.append(window)
            else:
                rejected.append({"window": window, "reason": auth.get("reason"), "code": auth.get("code")})

        strict = bool(pick(data, "strict", "all_or_nothing"))
        ok = not rejected if strict else bool(accepted)
        if job is not None:
            job["归档版本"] = version
            job["archive_version"] = version
            job["授权编号"] = (accepted[0].get("grant_id") if accepted else rejected[0]["window"].get("grant_id")) if (accepted or rejected) else None
            if accepted:
                job["授权清单"] = [w.get("grant_id") for w in accepted if w.get("grant_id")]
            job["授权状态"] = "部分通过" if accepted and rejected else (ACTIVE_GRANT_STATUS if accepted else "拒绝")
            job["回填清单"] = [w.get("window_id") for w in accepted]
        return {
            "ok": ok,
            "code": "TASK_ACCEPTED_WITH_REJECTIONS" if accepted and rejected else ("TASK_ACCEPTED" if accepted else "TASK_REJECTED"),
            "task_id": data.get("task_id") or entry_id,
            "table": table,
            "archive_version": version,
            "authorization": base_auth,
            "accepted_windows": accepted,
            "rejected_windows": rejected,
            "rejected": rejected,
            "windows": accepted + [item["window"] for item in rejected],
            "manifest": store._backfill_manifest,
        }

    accept_backfill_task = accept_task
    accept_backfill = accept_task

    def _segment_requests(self, data: dict[str, Any]) -> list[Any]:
        segments = pick(data, "segments", "windows", "ranges", "分段", "窗口列表")
        if segments:
            return list(segments) if isinstance(segments, (list, tuple)) else as_list(segments)
        partitions = pick(data, "partitions", "partition_list", "分区列表", "请求分区")
        if partitions:
            return as_list(partitions)
        partition = self._partition(data)
        if partition:
            return [partition]
        return []

    def _segment_data(self, segment: Any, index: int, table: str, version: str, parent: dict[str, Any]) -> dict[str, Any]:
        if isinstance(segment, dict):
            item = dict(segment)
        else:
            item = {"partition": segment}
        item.setdefault("table_name", table)
        item.setdefault("archive_version", version)
        item.setdefault("task_id", parent.get("task_id") or parent.get("entry_id"))
        item.setdefault("row_count", parent.get("row_count"))
        item["_segment_index"] = index
        return item

    def _build_window(self, segment_data: dict[str, Any], auth: dict[str, Any], index: int, parent: dict[str, Any], now: float) -> dict[str, Any]:
        partition = self._partition(segment_data)
        raw_partition = self._partition_raw(segment_data)
        partition_text = str(raw_partition) if raw_partition not in (None, "", []) else partition
        segment_id = pick(segment_data, "segment_id", "window_key", "第一段", "分段编号") or f"SEG-{index}"
        scope = {
            "segment_id": segment_id,
            "partition": partition,
            "partition_text": partition_text,
            "partitions": [partition_text] if partition_text else [],
            "range_start": pick(segment_data, "range_start", "start", "起始位点", "回填起始时间"),
            "range_end": pick(segment_data, "range_end", "end", "结束位点", "回填结束时间"),
            "executed_partitions": [partition_text] if partition_text else [],
        }
        grant = auth.get("grant") if auth.get("ok") else (auth.get("grant") or {})
        window_pk = len([w for w in store._backfill_manifest if w.get("entry_type", "window") == "window"]) + 1
        window_code = pick(segment_data, "window_id", "window_no") or f"BW-{window_pk:04d}"
        return {
            "entry_type": "window",
            "type": "backfill_window",
            "kind": "segment",
            "id": window_pk,
            "window_id": window_code,
            "segment_id": segment_id,
            "task_id": segment_data.get("task_id"),
            "table_name": self._table(segment_data),
            "目标表": pick(segment_data, "target_table", "目标表") or self._table(segment_data),
            "archive_table": self._table(segment_data),
            "archive_version": self._version(segment_data),
            "归档版本": self._version(segment_data),
            "partition": partition,
            "requested_partition": partition_text,
            "partition_text": partition_text,
            "目标分区": partition_text or partition,
            "scope": scope,
            "executed_scope": copy.deepcopy(scope),
            "grant_id": auth.get("grant_id") or grant.get("grant_id"),
            "grant_pk": grant.get("id"),
            "authorization_snapshot": auth.get("authorization_snapshot"),
            "permit": auth.get("permit"),
            "permit_expires_at": (auth.get("permit") or {}).get("expires_at"),
            "revocation_policy": (auth.get("permit") or {}).get("revocation_policy", self.DEFAULT_REVOCATION_POLICY),
            "status": "可领取" if auth.get("ok") else "已拒绝",
            "state": "claimable" if auth.get("ok") else "rejected",
            "claimable": bool(auth.get("ok")),
            "reject_reason": None if auth.get("ok") else auth.get("reason"),
            "reject_code": None if auth.get("ok") else auth.get("code"),
            "created_at": now,
        }

    def request_partition(self, payload: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        data = _data(payload=payload, **kwargs)
        auth = self.authorize(data)
        entry = {
            "entry_type": "partition_request",
            "type": "partition_application",
            "table_name": self._table(data),
            "archive_version": self._version(data),
            "partition": self._partition(data),
            "status": "已授权" if auth.get("ok") else "已拒绝",
            "reason": auth.get("reason", ""),
            "code": auth.get("code", ""),
            "requested_at": self._now(data),
        }
        store.add_backfill_manifest_entry(entry)
        return {**auth, "request": entry}

    apply_for_partition = request_partition

    def _resolve_window(self, target_id: Any, data: dict[str, Any] | None = None) -> dict[str, Any] | None:
        data = data or {}
        direct = store.find_backfill_window(target_id)
        if direct is not None:
            return direct

        segment_ref = pick(data, "segment_id", "segment", "segment_key", "分段编号", "第一段", "第二段")
        task_ref = pick(data, "task_id", "task_pk", "任务编号")
        target_text = None if target_id is None else str(target_id)

        # 支持 claim_segment(task_id, segment_id) 这种两个位置参数的调用。
        if target_text is not None and segment_ref is not None:
            pair = [
                w for w in store._backfill_windows
                if str(w.get("task_id")) == target_text and str(w.get("segment_id")) == str(segment_ref)
            ]
            if pair:
                return pair[-1]

        def identity_match(window: dict[str, Any]) -> bool:
            refs = {str(window.get("id")), str(window.get("window_id")), str(window.get("segment_id"))}
            return target_text in refs or (segment_ref is not None and str(window.get("segment_id")) == str(segment_ref))

        candidates = [w for w in store._backfill_windows if identity_match(w)]
        if task_ref is not None:
            scoped = [w for w in candidates if str(w.get("task_id")) == str(task_ref)]
            if scoped:
                candidates = scoped
        if candidates:
            return candidates[-1]

        # 支持 claim_segment(task_id, segment_id) 这种两个位置参数的调用。
        if target_text is not None and segment_ref is not None:
            pair = [
                w for w in store._backfill_windows
                if str(w.get("task_id")) == target_text and str(w.get("segment_id")) == str(segment_ref)
            ]
            if pair:
                return pair[-1]
        if target_text is not None and task_ref is None:
            by_task = [w for w in store._backfill_windows if str(w.get("task_id")) == target_text]
            partition = self._partition(data)
            if partition:
                by_partition = [w for w in by_task if partition_matches(w.get("partition"), partition)]
                if by_partition:
                    return by_partition[-1]
            if len(by_task) == 1:
                return by_task[0]
        return None

    def claim_window(self, window_id: Any = None, payload: dict[str, Any] | str | None = None, **kwargs: Any) -> dict[str, Any]:
        scalar_segment = payload if not isinstance(payload, dict) and payload is not None else None
        data_payload = payload if isinstance(payload, dict) else None
        data = _data(window_id if isinstance(window_id, dict) else None, data_payload, **kwargs)
        if scalar_segment is not None:
            data.setdefault("segment_id", scalar_segment)
        now = self._now(data)
        target_id = window_id if not isinstance(window_id, dict) else (window_id.get("id") or window_id.get("window_id"))
        target_id = target_id or pick(data, "window_id", "id", "窗口编号")
        window = self._resolve_window(target_id, data) if target_id is not None or pick(data, "segment_id") else None

        if window is None and (self._table(data) and self._version(data)):
            auth = self.authorize({**data, "action": "claim", "now": now})
            index = int(pick(data, "segment_index", "_segment_index") or 1)
            window = self._build_window(data, auth, index, data, now)
            store.add_backfill_manifest_entry(window)
            if not auth.get("ok"):
                return {**auth, "window": window, "segment": window}
        elif window is None:
            return {"ok": False, "code": "WINDOW_NOT_FOUND", "reason": f"回填窗口 {target_id} 不存在"}

        if window.get("status") in ("已确认", "confirmed"):
            return {"ok": True, "idempotent": True, "window": window, "segment": window, "message": "窗口已确认"}
        if window.get("status") in ("已撤回", "withdrawn"):
            return {"ok": False, "code": "WINDOW_WITHDRAWN", "reason": "窗口已撤回，不能重新领取", "window": window, "segment": window}

        # 幂等：同一执行者重复领取时返回原窗口。
        worker = pick(data, "worker_id", "worker", "executor", "领取方")
        if window.get("claimed_by") and worker and window.get("claimed_by") == worker and window.get("status") in ("已领取", "写入中"):
            return {"ok": True, "idempotent": True, "window": window, "segment": window, "window_id": window.get("window_id")}

        auth = self.authorize({
            "table_name": window.get("table_name"),
            "archive_version": window.get("archive_version"),
            "partition": window.get("partition"),
            "grant_id": window.get("grant_id"),
            "now": now,
            "action": "claim",
        })
        if not auth.get("ok"):
            window.update({
                "status": "已拒绝",
                "state": "rejected",
                "claimable": False,
                "reject_reason": auth.get("reason"),
                "reject_code": auth.get("code"),
                "claim_attempted_at": now,
            })
            return {**auth, "window": window, "segment": window, "window_id": window.get("window_id")}

        permit = auth["permit"]
        window.update({
            "status": "写入中" if data.get("start", True) or kwargs.get("start") is True else "已领取",
            "state": "writing" if data.get("start", True) or kwargs.get("start") is True else "claimed",
            "claimable": False,
            "claimed_at": now,
            "claimed_by": worker,
            "permit": permit,
            "permit_expires_at": permit.get("expires_at"),
            "revocation_policy": permit.get("revocation_policy", self.DEFAULT_REVOCATION_POLICY),
            "authorization_snapshot": auth.get("authorization_snapshot"),
        })
        if window["status"] == "写入中":
            write_result = self._create_write(window, now=now, provisional=True, row_count=pick(data, "row_count", "rows_written"))
            window["write_record_id"] = write_result["record"]["write_id"]
        return {"ok": True, "code": "WINDOW_CLAIMED", "window": window, "segment": window, "window_id": window.get("window_id"), "segment_id": window.get("segment_id"), "permit": permit}

    claim_backfill_window = claim_window
    claim_backfill_segment = claim_window
    claim_segment = claim_window
    acquire_window = claim_window
    acquire_segment = claim_window
    claim_next_window = claim_window

    def start_segment(self, window_id: Any = None, payload: dict[str, Any] | str | None = None, **kwargs: Any) -> dict[str, Any]:
        scalar_segment = payload if not isinstance(payload, dict) and payload is not None else None
        data_payload = payload if isinstance(payload, dict) else None
        data = _data(window_id if isinstance(window_id, dict) else None, data_payload, **kwargs)
        if scalar_segment is not None:
            data.setdefault("segment_id", scalar_segment)
        target_id = window_id if not isinstance(window_id, dict) else None
        result = self.claim_window(target_id, {**data, "start": False}, **{k: v for k, v in kwargs.items() if k != "start"})
        if not result.get("ok"):
            return result
        window = result["window"]
        now = self._now(data)
        if window.get("status") == "写入中":
            return {"ok": True, "idempotent": True, "window": window, "segment": window, "write_record_id": window.get("write_record_id")}
        window.update({"status": "写入中", "state": "writing", "started_at": now})
        write_result = self._create_write(window, now=now, provisional=True, row_count=pick(data, "row_count", "rows_written"))
        window["write_record_id"] = write_result["record"]["write_id"]
        return {"ok": True, "code": "SEGMENT_STARTED", "window": window, "segment": window, "write_record": write_result["record"]}

    start_backfill_segment = start_segment
    begin_segment = start_segment
    begin_backfill_segment = start_segment

    def _create_write(self, window: dict[str, Any], *, now: float, provisional: bool, row_count: Any = None) -> dict[str, Any]:
        existing_id = window.get("write_record_id")
        if existing_id:
            existing = next((r for r in store._backfill_records if r.get("write_id") == existing_id), None)
            if existing is not None:
                return {"record": existing, "created": False}

        try:
            count = int(row_count) if row_count is not None else 20
        except (TypeError, ValueError):
            count = 20
        scope = copy.deepcopy(window.get("scope") or {})
        scope.update({
            "window_id": window.get("window_id"),
            "segment_id": window.get("segment_id"),
            "partition": window.get("partition"),
            "executed_partitions": scope.get("executed_partitions") or ([window.get("partition")] if window.get("partition") else []),
        })
        snapshot = copy.deepcopy(window.get("authorization_snapshot") or {})
        write_pk = len(store._backfill_records) + 1
        write_id = f"BW-{write_pk:04d}"
        record = {
            "entry_type": "backfill_write",
            "type": "backfill_write_record",
            "id": write_pk,
            "write_id": write_id,
            "window_id": window.get("window_id"),
            "segment_id": window.get("segment_id"),
            "task_id": window.get("task_id"),
            "table_name": window.get("table_name"),
            "table": window.get("table_name"),
            "target_table": window.get("目标表") or window.get("table_name"),
            "目标表": window.get("目标表") or window.get("table_name"),
            "archive_table": window.get("archive_table") or window.get("table_name"),
            "archive_version": window.get("archive_version"),
            "归档版本": window.get("归档版本") or window.get("archive_version"),
            "version": window.get("archive_version"),
            "partition": window.get("partition"),
            "目标分区": window.get("partition"),
            "executed_partitions": scope.get("executed_partitions") or ([window.get("partition")] if window.get("partition") else []),
            "executed_scope": scope,
            "已执行范围": scope,
            "executed_range": scope,
            "actual_scope": scope,
            "actual_range": scope,
            "authorized_scope": copy.deepcopy(snapshot.get("scope") or {}),
            "grant_id": window.get("grant_id"),
            "authorization_basis": snapshot,
            "grant_snapshot": snapshot,
            "authorization": snapshot,
            "grant": snapshot,
            "授权依据": snapshot,
            "authorized_at": snapshot.get("authorized_at") or (window.get("permit") or {}).get("authorized_at"),
            "grant_authorized_at": snapshot.get("authorized_at") or (window.get("permit") or {}).get("authorized_at"),
            "granted_at": snapshot.get("granted_at") or snapshot.get("authorized_at"),
            "authorization_time": snapshot.get("authorized_at"),
            "授权时点": snapshot.get("授权时点") or iso_time(snapshot.get("authorized_at")),
            "授权时间": snapshot.get("授权时间") or iso_time(snapshot.get("authorized_at")),
            "written_at": now,
            "executed_at": iso_time(now),
            "rows_written": count,
            "actual_write": True,
            "committed": not provisional,
            "status": "待确认" if provisional else "已写入",
            "requery_key": f"{window.get('table_name')}@{window.get('archive_version')}",
        }
        store.add_backfill_record(record)
        for index in range(count):
            store._backfill_written_rows.append({
                "write_id": write_id,
                "window_id": window.get("window_id"),
                "table_name": window.get("table_name"),
                "archive_version": window.get("archive_version"),
                "partition": window.get("partition"),
                "row_index": index,
                "written_at": now,
                "visible": not provisional,
                "committed": not provisional,
            })
        window["executed_scope"] = copy.deepcopy(scope)
        return {"record": record, "created": True}

    def confirm_segment(self, window_id: Any = None, payload: dict[str, Any] | str | None = None, **kwargs: Any) -> dict[str, Any]:
        scalar_segment = payload if not isinstance(payload, dict) and payload is not None else None
        data_payload = payload if isinstance(payload, dict) else None
        data = _data(window_id if isinstance(window_id, dict) else None, data_payload, **kwargs)
        if scalar_segment is not None:
            data.setdefault("segment_id", scalar_segment)
        now = self._now(data)
        target_id = window_id if not isinstance(window_id, dict) else (window_id.get("id") or window_id.get("window_id"))
        target_id = target_id or pick(data, "window_id", "id", "segment_id", "窗口编号")
        window = self._resolve_window(target_id, data) if target_id is not None else None
        if window is None and self._table(data) and self._version(data):
            claim = self.claim_window(data)
            if not claim.get("ok"):
                return claim
            window = claim["window"]
        if window is None:
            return {"ok": False, "code": "WINDOW_NOT_FOUND", "reason": f"待确认回填窗口 {target_id} 不存在"}

        if window.get("status") == "已确认":
            record = next((r for r in store._backfill_records if r.get("write_id") == window.get("write_record_id")), None)
            return {"ok": True, "idempotent": True, "window": window, "write_record": record, "actual_write": record}
        if window.get("status") in ("已撤回", "withdrawn", "已拒绝", "rejected"):
            return {
                "ok": False,
                "code": window.get("reject_code") or "WINDOW_NOT_CONFIRMABLE",
                "reason": window.get("reject_reason") or f"窗口状态为 {window.get('status')}，不能确认",
                "window": window,
            }
        if window.get("status") in ("可领取", "claimable", "已授权"):
            claim = self.claim_window(window, {"start": True, "now": now})
            if not claim.get("ok"):
                return claim
            window = claim["window"]
        if window.get("status") not in ("已领取", "写入中", "claimed", "writing"):
            return {"ok": False, "code": "WINDOW_NOT_STARTED", "reason": f"窗口状态 {window.get('status')} 不能确认", "window": window}

        if not window.get("write_record_id"):
            write_result = self._create_write(window, now=now, provisional=True)
            window["write_record_id"] = write_result["record"]["write_id"]
        record = next((r for r in store._backfill_records if r.get("write_id") == window.get("write_record_id")), None)

        grant = store.find_archive_grant(window.get("grant_id")) if window.get("grant_id") is not None else None
        permit_expires = window.get("permit_expires_at")
        revoked = bool(grant and (grant.get("revoked") or grant.get("state") == "revoked"))
        expired = bool(
            (grant and grant.get("expires_at") is not None and now > float(grant["expires_at"]))
            or (permit_expires is not None and now > float(permit_expires))
        )
        policy = window.get("revocation_policy") or self.DEFAULT_REVOCATION_POLICY
        if revoked and (policy == "stop" or data.get("stop_on_revoke") is True):
            withdrawn = self.withdraw_window(window, reason="授权已撤销，按停止策略撤回在途第一段", now=now)
            return {
                "ok": False,
                "code": "REVOKED_IN_FLIGHT_WITHDRAWN",
                "reason": "确认前授权已撤销；该已开始但未确认的窗口已停止并撤回实际写入",
                "window": withdrawn.get("window", window),
                "withdrawal": withdrawn,
            }
        if expired:
            withdrawn = self.withdraw_window(window, reason="已签发许可已超过有效时段，撤回未确认窗口", now=now)
            return {
                "ok": False,
                "code": "PERMIT_EXPIRED_WITHDRAWN",
                "reason": "回填许可已过期，未确认窗口的实际写入已撤回",
                "window": withdrawn.get("window", window),
                "withdrawal": withdrawn,
            }
        if revoked and policy != "complete_within_permit":
            withdrawn = self.withdraw_window(window, reason="授权已撤销，撤回在途窗口", now=now)
            return {"ok": False, "code": "GRANT_REVOKED", "reason": "授权已撤销且冲突策略不允许在途完成，窗口已撤回", "withdrawal": withdrawn}

        # 提交实际写入。授权依据仍指向领取/写入时点的深拷贝，不随后续撤销改变。
        if record is not None:
            record.update({
                "committed": True,
                "status": "已写入",
                "confirmed_at": now,
                "confirmed_time": iso_time(now),
            })
            for row in store._backfill_written_rows:
                if row.get("write_id") == record.get("write_id"):
                    row["visible"] = True
                    row["committed"] = True
        window.update({
            "status": "已确认",
            "state": "confirmed",
            "confirmed_at": now,
            "confirmation_time": iso_time(now),
            "completed_scope": copy.deepcopy(window.get("executed_scope") or {}),
            "revoked_but_completed_under_permit": revoked,
        })
        return {
            "ok": True,
            "code": "SEGMENT_CONFIRMED" if not revoked else "CONFIRMED_UNDER_EXISTING_PERMIT",
            "window": window,
            "segment": window,
            "write_record": record,
            "actual_write": record,
            "message": "第一段已在原签发许可有效时段内完成，授权时点和执行范围已留存" if revoked else "分段确认完成",
        }

    confirm_backfill_segment = confirm_segment
    commit_segment = confirm_segment
    confirm_window = confirm_segment
    confirm_backfill = confirm_segment
    complete_segment = confirm_segment
    complete_window = confirm_segment

    def withdraw_window(
        self, window_id: Any = None, reason: str | None = None,
        payload: dict[str, Any] | str | None = None, **kwargs: Any,
    ) -> dict[str, Any]:
        scalar_segment = payload if not isinstance(payload, dict) and payload is not None else None
        data_payload = payload if isinstance(payload, dict) else None
        data = _data(window_id if isinstance(window_id, dict) else None, data_payload, **kwargs)
        if scalar_segment is not None:
            data.setdefault("segment_id", scalar_segment)
        now = self._now(data)
        target = window_id if isinstance(window_id, dict) else pick(data, "window_id", "id", "窗口编号")
        window = target if isinstance(target, dict) else self._resolve_window(target, data)
        if window is None:
            return {"ok": False, "code": "WINDOW_NOT_FOUND", "reason": "待撤回窗口不存在"}
        if window.get("status") == "已确认":
            return {"ok": False, "code": "COMPLETED_RECORD_RETAINED", "reason": "已完成并留存授权依据的历史记录不能按撤回未确认窗口处理"}
        reason = reason or pick(data, "reason", "撤回原因") or "窗口已撤回"
        window.update({
            "status": "已撤回",
            "state": "withdrawn",
            "claimable": False,
            "withdrawn_at": now,
            "withdraw_reason": reason,
        })
        record = None
        if window.get("write_record_id"):
            record = next((r for r in store._backfill_records if r.get("write_id") == window.get("write_record_id")), None)
            if record is not None:
                record.update({"status": "已撤回", "committed": False, "withdrawn_at": now, "withdraw_reason": reason})
                for row in store._backfill_written_rows:
                    if row.get("write_id") == record.get("write_id"):
                        row["visible"] = False
                        row["committed"] = False
                        row["withdrawn"] = True
        return {"ok": True, "code": "WINDOW_WITHDRAWN", "window": window, "write_record": record, "withdrawn": True}

    withdraw_segment = withdraw_window
    stop_window = withdraw_window
    stop_segment = withdraw_window
    abort_segment = withdraw_window
    rollback_segment = withdraw_window

    # ---------- 回查 ----------
    def list_write_records(self, payload: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        data = _data(payload=payload, **kwargs)
        table = self._table(data) or None
        version = self._version(data) or None
        items = store.backfill_records_for_version(table, version)
        status = pick(data, "status", "write_status")
        include_withdrawn = bool(pick(data, "include_withdrawn", "all", "include_audit"))
        if status and status != "all":
            items = [r for r in items if r.get("status") == status or r.get("write_status") == status]
        elif not include_withdrawn and not data.get("include_unconfirmed"):
            items = [r for r in items if r.get("committed") is True and r.get("status") != "已撤回"]
        return {"ok": True, "items": items, "records": items, "writes": items, "results": items, "total": len(items)}

    query_writes = list_write_records
    query_backfill_records = list_write_records
    list_backfill_records = list_write_records
    get_write_records = list_write_records
    get_backfill_records = list_write_records
    get_records = list_write_records

    def find_records_by_version(self, first: Any = None, second: Any = None, **kwargs: Any) -> list[dict[str, Any]]:
        # 版本回查首先是审计入口：即使分段随后撤回，也保留可按原版本查到的写入/撤回凭证。
        if isinstance(first, dict):
            query = dict(first)
            query.update(kwargs)
            query.setdefault("include_withdrawn", True)
            query.setdefault("include_unconfirmed", True)
            return self.list_write_records(query)["items"]

        explicit_version = kwargs.pop("archive_version", None) or kwargs.pop("version", None) or kwargs.pop("归档版本", None)
        explicit_table = kwargs.pop("table_name", None) or kwargs.pop("table", None) or kwargs.pop("表名", None)
        if explicit_version is not None or explicit_table is not None:
            version = explicit_version if explicit_version is not None else first
            table = explicit_table if explicit_table is not None else (first if explicit_version is not None else second)
            query = {"table_name": table, "archive_version": version, **kwargs}
            query.setdefault("include_withdrawn", True)
            query.setdefault("include_unconfirmed", True)
            return self.list_write_records(query)["items"]

        # 兼容 (version, table) 和 (table, version) 两种常见位置参数顺序。
        query_v_first = {"table_name": second, "archive_version": first, **kwargs}
        query_v_first.setdefault("include_withdrawn", True)
        query_v_first.setdefault("include_unconfirmed", True)
        items_v_first = self.list_write_records(query_v_first)["items"]
        if items_v_first or second is None:
            return items_v_first

        query_t_first = {"table_name": first, "archive_version": second, **kwargs}
        query_t_first.setdefault("include_withdrawn", True)
        query_t_first.setdefault("include_unconfirmed", True)
        return self.list_write_records(query_t_first)["items"]

    get_records_by_version = find_records_by_version
    find_writes_by_version = find_records_by_version

    def query_writes_by_version(self, first: Any = None, second: Any = None, **kwargs: Any) -> dict[str, Any]:
        # 沿用服务 dict 契约；位置参数顺序交给 find_records_by_version 兼容。
        items = self.find_records_by_version(first, second, **kwargs)
        return {"ok": True, "items": items, "records": items, "writes": items, "results": items, "total": len(items)}

    def records_for_version(self, table_name: str | None, archive_version: str, **kwargs: Any) -> list[dict[str, Any]]:
        return self.list_write_records({"table_name": table_name, "archive_version": archive_version, **kwargs})["items"]

    def manifest(self, payload: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "items": list(store._backfill_manifest), "total": len(store._backfill_manifest)}

    backfill_manifest = manifest


def _split_sources(value: Any) -> list[str]:
    if value is None:
        return []
    return [item.strip() for item in re.split(r"[,，、;；]", str(value)) if item.strip()]


archive_access = ArchiveAccessService()
