"""归档版本授权 → 回填窗口 → 版本化写入审计的验收测试。"""
from __future__ import annotations

import time
import uuid

from app.services import service_for
from app.services.archive_access import archive_access
from app.store import store


def _suffix() -> str:
    return uuid.uuid4().hex[:10]


def test_archive_grant_scopes_table_version_partition_and_path_independently():
    suffix = _suffix()
    table_a = f"甲表_{suffix}"
    table_b = f"乙表_{suffix}"
    version = "archive-20260501"
    path_a = f"s3://cold/prefix/orders_{suffix}"
    # 故意构造“同前缀但相似”的路径，不能通过 startswith 获得授权。
    path_b = f"s3://cold/prefix/orders_{suffix}_backup"

    archive_access.register_archive_version(
        table_name=table_a, archive_version=version, archive_path=path_a
    )
    archive_access.register_archive_version(
        table_name=table_b, archive_version=version, archive_path=path_b
    )
    grant = archive_access.grant_archive_version(
        table_name=table_a,
        archive_version=version,
        partitions=["5月1日"],
        expires_at=time.time() + 3600,
        granted_by="acceptance",
    )
    assert grant["ok"] is True
    assert grant["grant"]["permission"] == "read"
    assert grant["grant"]["read_only"] is True

    allowed = archive_access.authorize(
        table_name=table_a, archive_version=version, partition="2026-05-01", path=path_a
    )
    assert allowed["ok"] is True

    denied_b = archive_access.authorize(
        table_name=table_b, archive_version=version, partition="2026-05-01", path=path_b
    )
    assert denied_b["ok"] is False
    assert denied_b["code"] == "GRANT_NOT_FOUND"

    denied_may2 = archive_access.authorize(
        table_name=table_a, archive_version=version, partition="2026-05-02"
    )
    assert denied_may2["ok"] is False
    assert denied_may2["code"] == "PARTITION_NOT_AUTHORIZED"
    assert "2026-05-02" in denied_may2["reason"]

    write_denied = archive_access.write_archive_version(
        table_name=table_a, archive_version=version, partition="2026-05-01"
    )
    assert write_denied["ok"] is False
    assert write_denied["code"] == "ARCHIVE_WRITE_FORBIDDEN"


def test_revocation_blocks_new_claim_but_valid_permit_completes_in_flight_write():
    suffix = _suffix()
    table = f"backfill_a_{suffix}"
    other_table = f"backfill_b_{suffix}"
    version = f"v-{suffix}"
    path = f"s3://cold/archive/{suffix}/table"
    other_path = f"s3://cold/archive/{suffix}/table_similar"

    archive_access.register_archive_version(table_name=table, archive_version=version, archive_path=path)
    archive_access.register_archive_version(
        table_name=other_table, archive_version=version, archive_path=other_path
    )
    grant = archive_access.create_grant(
        table_name=table,
        archive_version=version,
        archive_path=path,
        partitions=["2026-05-01"],
        expires_at=time.time() + 3600,
    )
    grant_id = grant["grant_id"]

    accepted = archive_access.accept_backfill_task(
        table_name=table,
        archive_version=version,
        task_id=f"TASK-{suffix}",
        segments=[
            {"segment_id": f"first-{suffix}", "partition": "2026-05-01"},
            {"segment_id": f"second-{suffix}", "partition": "2026-05-01"},
        ],
    )
    assert accepted["ok"] is True
    assert len(accepted["accepted_windows"]) == 2
    first, second = accepted["accepted_windows"]

    started = archive_access.start_segment(first["window_id"], row_count=7)
    assert started["ok"] is True
    assert started["write_record"]["status"] == "待确认"

    # 授权结果已同步到回填清单。
    assert any(
        item.get("entry_type") == "authorization" and item.get("grant_id") == grant_id
        for item in store._backfill_manifest
    )

    revoked = archive_access.revoke_archive_grant(grant_id, reason="验收撤回")
    assert revoked["ok"] is True

    confirmed = archive_access.confirm_segment(first["window_id"])
    assert confirmed["ok"] is True
    assert confirmed["code"] == "CONFIRMED_UNDER_EXISTING_PERMIT"
    write_record = confirmed["actual_write"]
    assert write_record["committed"] is True
    assert write_record["rows_written"] == 7
    assert write_record["authorized_at"] == grant["grant"]["authorized_at"]
    assert write_record["authorization_basis"]["grant_id"] == grant_id
    assert write_record["executed_scope"]["segment_id"] == f"first-{suffix}"
    assert write_record["executed_scope"]["executed_partitions"] == ["2026-05-01"]

    second_claim = archive_access.claim_window(second["window_id"])
    assert second_claim["ok"] is False
    assert second_claim["code"] == "GRANT_REVOKED"
    assert "撤销" in second_claim["reason"]

    # 相似路径的乙表不能因为甲表授权或本次写入而获得授权/回查结果。
    other_claim = archive_access.claim_window(
        table_name=other_table,
        archive_version=version,
        partition="2026-05-01",
        archive_path=other_path,
    )
    assert other_claim["ok"] is False
    assert other_claim["code"] == "GRANT_NOT_FOUND"

    records = archive_access.find_records_by_version(version, table)
    assert len(records) == 1
    assert records[0]["write_id"] == write_record["write_id"]
    assert records[0]["authorization_basis"]["status"] == "有效"

    # 授权后来已撤销，但已完成历史记录中的授权依据仍是授权时点的有效快照。
    current_grant = store.find_archive_grant(grant_id)
    assert current_grant["status"] == "已撤销"
    assert records[0]["authorization_basis"]["status"] == "有效"

    assert archive_access.find_records_by_version(version, other_table) == []


def test_stop_policy_withdraws_unconfirmed_segment():
    suffix = _suffix()
    table = f"stop_table_{suffix}"
    version = f"stop-v-{suffix}"
    grant = archive_access.create_grant(
        table_name=table,
        archive_version=version,
        partitions=["2026-05-01"],
        expires_at=time.time() + 3600,
        revocation_policy="stop",
    )
    accepted = archive_access.accept_task(
        table_name=table,
        archive_version=version,
        segments=[{"segment_id": f"first-{suffix}", "partition": "2026-05-01"}],
    )
    window_id = accepted["accepted_windows"][0]["window_id"]
    archive_access.start_segment(window_id, row_count=3)
    revoked = archive_access.revoke_grant(grant["grant_id"])
    assert len(revoked["stopped_windows"]) == 1

    confirmed = archive_access.confirm_segment(window_id)
    assert confirmed["ok"] is False
    assert confirmed["window"]["status"] == "已撤回"

    records = archive_access.list_write_records(
        table_name=table, archive_version=version, include_withdrawn=True
    )["items"]
    assert len(records) == 1
    assert records[0]["status"] == "已撤回"
    assert records[0]["committed"] is False
    audit_records = archive_access.find_records_by_version(version, table)
    assert len(audit_records) == 1
    assert audit_records[0]["status"] == "已撤回"
    # 正式的已提交写入查询仍不包含已撤回记录。
    assert archive_access.records_for_version(table, version) == []


def test_services_expose_archive_and_backfill_operations():
    cold = service_for("cold_archive")
    backfill = service_for("backfill_job")
    assert hasattr(cold, "grant_archive_version")
    assert hasattr(cold, "revoke_archive_grant")
    assert hasattr(backfill, "accept_backfill_task")
    assert hasattr(backfill, "confirm_segment")
    assert hasattr(backfill, "get_records_by_version")
