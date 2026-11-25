"""归档版本授权和回填窗口的显式 HTTP 接口。

通用资源路由仍保留；这里提供不需要绑定某个 cold_archive/backfill_job 行 ID 的
领域接口，便于授权中心、回填调度器和审计查询直接调用。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.services.archive_access import archive_access
from app.store import store

router = APIRouter(prefix="/api", tags=["归档版本授权与回填审计"])


async def _json(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {"payload": payload}


@router.post("/archive-versions")
@router.post("/archive_versions")
async def register_archive_version(request: Request) -> dict[str, Any]:
    return archive_access.register_archive_version(await _json(request))


@router.post("/archive-grants")
@router.post("/archive_grants")
@router.post("/cold_archive/grants")
@router.post("/cold_archive/archive_grants")
@router.post("/cold_archive/grant")
@router.post("/archive-authorizations")
@router.post("/archive-permissions")
@router.post("/grants")
async def create_archive_grant(request: Request) -> dict[str, Any]:
    return archive_access.create_grant(await _json(request))


@router.get("/archive-grants")
@router.get("/archive_grants")
@router.get("/cold_archive/grants")
@router.get("/cold_archive/archive_grants")
@router.get("/archive-authorizations")
@router.get("/archive-permissions")
@router.get("/grants")
async def list_archive_grants(table_name: str | None = None, archive_version: str | None = None) -> dict[str, Any]:
    return archive_access.list_grants({"table_name": table_name, "archive_version": archive_version})


@router.get("/archive-grants/{grant_id}")
@router.get("/archive_grants/{grant_id}")
@router.get("/grants/{grant_id}")
async def get_archive_grant(grant_id: str) -> dict[str, Any]:
    grant = store.find_archive_grant(grant_id)
    if grant is None:
        return {"ok": False, "code": "GRANT_NOT_FOUND", "reason": f"授权 {grant_id} 不存在"}
    return {"ok": True, "grant": grant}


@router.post("/archive-grants/{grant_id}/revoke")
@router.post("/archive_grants/{grant_id}/revoke")
@router.post("/archive-grants/{grant_id}/withdraw")
@router.post("/grants/{grant_id}/revoke")
async def revoke_archive_grant(grant_id: str, request: Request) -> dict[str, Any]:
    payload = await _json(request)
    return archive_access.revoke_grant(grant_id, **payload)


@router.post("/archive-access/authorize")
@router.post("/archive_access/authorize")
async def authorize_archive_access(request: Request) -> dict[str, Any]:
    return archive_access.authorize(await _json(request))


@router.post("/archive-access/write-check")
async def check_archive_write(request: Request) -> dict[str, Any]:
    return archive_access.assert_archive_write_allowed(await _json(request))


@router.post("/backfill-tasks/accept")
@router.post("/backfill_tasks/accept")
@router.post("/backfill/accept")
@router.post("/backfill_job/accept")
@router.post("/backfill_job/accept_backfill_task")
async def accept_backfill_task(request: Request) -> dict[str, Any]:
    return archive_access.accept_task(await _json(request))


@router.post("/backfill-windows/claim")
@router.post("/backfill_windows/claim")
@router.post("/backfill-segments/claim")
@router.post("/backfill/claim")
@router.post("/backfill_job/claim_window")
@router.post("/backfill_job/claim_segment")
async def claim_backfill_window(request: Request) -> dict[str, Any]:
    return archive_access.claim_window(await _json(request))


@router.post("/backfill-windows/{window_id}/start")
@router.post("/backfill_windows/{window_id}/start")
async def start_backfill_window(window_id: str, request: Request) -> dict[str, Any]:
    return archive_access.start_segment(window_id, await _json(request))


@router.post("/backfill-windows/{window_id}/confirm")
@router.post("/backfill_windows/{window_id}/confirm")
@router.post("/backfill-segments/{window_id}/confirm")
@router.post("/backfill/segments/{window_id}/confirm")
async def confirm_backfill_window(window_id: str, request: Request) -> dict[str, Any]:
    return archive_access.confirm_segment(window_id, await _json(request))


@router.post("/backfill/confirm")
@router.post("/backfill_job/confirm_segment")
@router.post("/backfill_job/confirm_window")
async def confirm_backfill_from_body(request: Request) -> dict[str, Any]:
    return archive_access.confirm_segment(await _json(request))


@router.post("/backfill/start")
@router.post("/backfill_job/start_segment")
async def start_backfill_from_body(request: Request) -> dict[str, Any]:
    return archive_access.start_segment(await _json(request))


@router.post("/backfill/withdraw")
@router.post("/backfill_job/withdraw_segment")
async def withdraw_backfill_from_body(request: Request) -> dict[str, Any]:
    return archive_access.withdraw_window(await _json(request))


@router.post("/backfill-windows/{window_id}/withdraw")
@router.post("/backfill_windows/{window_id}/withdraw")
async def withdraw_backfill_window(window_id: str, request: Request) -> dict[str, Any]:
    return archive_access.withdraw_window(window_id, **(await _json(request)))


@router.get("/backfill-windows")
@router.get("/backfill_windows")
async def list_backfill_windows() -> dict[str, Any]:
    windows = [w for w in store._backfill_manifest if w.get("entry_type", "window") == "window"]
    return {"ok": True, "items": windows, "total": len(windows)}


@router.get("/backfill-manifest")
@router.get("/backfill_manifest")
@router.get("/backfill/checklist")
@router.get("/backfill_checklist")
async def get_backfill_manifest() -> dict[str, Any]:
    return archive_access.manifest()


@router.get("/backfill-records")
@router.get("/backfill_records")
@router.get("/backfill/records")
@router.get("/backfill_job/records")
@router.get("/backfill_job/records_by_version")
async def query_backfill_records(
    table_name: str | None = None,
    table: str | None = None,
    archive_version: str | None = None,
    version: str | None = None,
    include_withdrawn: bool = False,
) -> dict[str, Any]:
    return archive_access.list_write_records({
        "table_name": table_name or table,
        "archive_version": archive_version or version,
        "include_withdrawn": include_withdrawn,
    })
