"""Admin-only dashboard & management routes."""
from __future__ import annotations

import csv
import io
import json
import os
import zipfile
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.deps import get_current_user, require_admin
from app.gemini import test_connection
from app.agnes import test_connection_agnes, probe_agnes_key
from app import pool as keypool
from app import settings_store
from app.models import (
    AdminImageRecord,
    AdminSettings,
    AdminSettingsUpdate,
    AdminStats,
    ApiKey,
    ApiTestResult,
    GenerationTask,
    PagedImageRecords,
    PagedTasks,
    PoolKeyAddBody,
    PoolKeyOut,
    PoolTestResult,
    TaskSummary,
    TaskItem,
    User,
    UserRead,
)
from app.settings_store import get_public, update as update_settings

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _item_urls(item: TaskItem) -> tuple[list[str], str | None]:
    """Return (original_urls, output_url) for a TaskItem, honoring multi-angle."""
    orig = f"/storage/{item.original_path}" if item.original_path else None
    if item.original_paths:
        try:
            urls = [f"/storage/{p}" for p in json.loads(item.original_paths)]
        except Exception:
            urls = [orig] if orig else []
    else:
        urls = [orig] if orig else []
    output = f"/storage/{item.output_path}" if item.output_path else None
    return urls, output


# --------------------------------------------------------------------------- #
# API settings (key / base URL / model) -- admin only
# --------------------------------------------------------------------------- #
@router.get("/settings", response_model=AdminSettings)
async def get_settings(_admin: User = Depends(require_admin)) -> AdminSettings:
    """Return the current API configuration (masked key, never the raw key)."""
    return AdminSettings(**get_public())


@router.put("/settings", response_model=AdminSettings)
async def put_settings(
    body: AdminSettingsUpdate,
    _admin: User = Depends(require_admin),
) -> AdminSettings:
    """Persist admin-edited overrides. Empty fields are left unchanged."""
    pub = update_settings(
        api_key=body.gemini_api_key,
        gemini_base_url=body.gemini_base_url,
        gemini_model=body.gemini_model,
        enable_gemini=body.enable_gemini,
        agnes_api_key=body.agnes_api_key,
        agnes_base_url=body.agnes_base_url,
        agnes_size_tier=body.agnes_size_tier,
        agnes_user_tier=body.agnes_user_tier,
    )
    # 配置（agnes_api_key / agnes_extra_keys）变化后同步进 Key 池。
    await keypool.sync_system_keys_from_settings()
    return AdminSettings(**pub)


@router.post("/settings/test", response_model=ApiTestResult)
async def test_settings(_admin: User = Depends(require_admin)) -> ApiTestResult:
    """Ping the Gemini endpoint with the current key (quota-free model list)."""
    return ApiTestResult(**await test_connection())


@router.post("/settings/test-agnes", response_model=ApiTestResult)
async def test_settings_agnes(_admin: User = Depends(require_admin)) -> ApiTestResult:
    """Validate the Agnes key with a tiny, free generation (no file saved)."""
    return ApiTestResult(**await test_connection_agnes())


# --------------------------------------------------------------------------- #
# Agnes Key 池（管理员）
# --------------------------------------------------------------------------- #
async def _pool_key_out(row: ApiKey, username: str | None) -> PoolKeyOut:
    return PoolKeyOut(
        id=row.id,
        provider=row.provider,
        owner_username=username,
        source=row.source,
        masked=settings_store.mask_key(row.key_value),
        status=row.status,
        note=row.note,
        created_at=row.created_at,
        validated_at=row.validated_at,
        last_used_at=row.last_used_at,
    )


@router.get("/keys", response_model=list[PoolKeyOut])
async def list_pool_keys(
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[PoolKeyOut]:
    """Full key pool with owner username (system keys show owner NULL)."""
    rows = (
        await session.execute(
            select(ApiKey, User.username)
            .outerjoin(User, ApiKey.owner_user_id == User.id)
            .where(ApiKey.provider == "agnes")
            .order_by(ApiKey.source, ApiKey.id.desc())
        )
    ).all()
    return [await _pool_key_out(row, username) for row, username in rows]


@router.post("/keys", response_model=PoolKeyOut, status_code=201)
async def add_system_key(
    body: PoolKeyAddBody,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> PoolKeyOut:
    """Add an admin/system key to the pool (no live probe at insert time;
    use POST /keys/{id}/test afterwards to verify it)."""
    raw = (body.api_key or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="请粘贴 Agnes API Key。")
    dup = (
        await session.execute(
            select(ApiKey).where(
                ApiKey.provider == "agnes", ApiKey.key_value == raw
            )
        )
    ).scalars().first()
    if dup is not None:
        owner = None
        if dup.owner_user_id:
            u = await session.get(User, dup.owner_user_id)
            owner = u.username if u else str(dup.owner_user_id)
        raise HTTPException(
            status_code=409,
            detail="该 Key 已在池中（"
            + (owner or "系统")
            + " 添加），无需重复录入。",
        )
    row = ApiKey(
        provider="agnes",
        source="system",
        owner_user_id=None,
        key_value=raw,
        status="valid",
        note=(body.note or "").strip() or "后台添加",
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return await _pool_key_out(row, None)


@router.post("/keys/{key_id}/test", response_model=PoolTestResult)
async def test_pool_key(
    key_id: int,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> PoolTestResult:
    """Live-probe one stored pool key and update its status."""
    row = await session.get(ApiKey, key_id)
    if row is None or row.provider != "agnes":
        raise HTTPException(status_code=404, detail="Key 不存在。")
    probe = await probe_agnes_key(api_key=row.key_value)
    ok = bool(probe.get("ok"))
    auth_failed = bool(probe.get("auth_failed"))
    if ok:
        row.status = "valid"
        row.note = (row.note or "").replace("；", "").strip()
        row.validated_at = keypool._utcnow()
    elif auth_failed:
        row.status = "invalid"
        row.note = f"校验被服务端拒绝：{str(probe.get('message',''))[:200]}"
    # 429 / 网络抖动：不轻易改状态，仅刷新校验时间戳方便排查。
    await session.commit()
    return PoolTestResult(
        ok=ok,
        status=int(probe.get("status") or 0),
        message=str(probe.get("message", "")),
        auth_failed=auth_failed,
    )


@router.delete("/keys/{key_id}")
async def delete_pool_key(
    key_id: int,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Revoke any pool key (system or a user's bound key)."""
    row = await session.get(ApiKey, key_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Key 不存在。")
    await session.delete(row)
    await session.commit()
    return {"ok": True, "deleted_key_id": row.id}


@router.get("/dashboard/stats", response_model=AdminStats)
async def dashboard_stats(
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> AdminStats:
    """Aggregate counters and billing totals across the whole system."""
    total_users = (await session.execute(select(func.count(User.id)))).scalar() or 0
    row = (
        await session.execute(
            select(
                func.coalesce(func.sum(User.total_api_calls), 0),
                func.coalesce(func.sum(User.total_images_generated), 0),
                func.coalesce(func.sum(User.total_cost_usd), 0.0),
            )
        )
    ).first()
    # 按模型拆分累计消耗（来自每张图片的 cost_usd 汇总）。
    cost_rows = (
        await session.execute(
            select(GenerationTask.model, func.coalesce(func.sum(TaskItem.cost_usd), 0.0))
            .join(TaskItem, TaskItem.task_id == GenerationTask.id)
            .group_by(GenerationTask.model)
        )
    ).all()
    cost_by_model = {
        (m or "unknown"): round(float(v), 6) for m, v in cost_rows
    }
    return AdminStats(
        total_users=total_users,
        total_api_calls=int(row[0]),
        total_images_generated=int(row[1]),
        total_cost_usd=round(float(row[2]), 6),
        cost_by_model=cost_by_model,
    )


@router.get("/users", response_model=list[UserRead])
async def list_users(
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[UserRead]:
    """All employee accounts with their usage counters."""
    users = (
        await session.execute(select(User).order_by(desc(User.created_at)))
    ).scalars().all()
    return [UserRead.model_validate(u) for u in users]


@router.get("/tasks/all", response_model=PagedTasks)
async def all_tasks(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> PagedTasks:
    """Paginated view of every task across all employees (with owner username)."""
    total = (await session.execute(select(func.count(GenerationTask.id)))).scalar() or 0

    rows = (
        await session.execute(
            select(GenerationTask, User.username)
            .join(User, GenerationTask.user_id == User.id)
            .order_by(desc(GenerationTask.created_at))
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()

    items = []
    for task, username in rows:
        from app.routes import tasks as tasks_route
        s = tasks_route._summary(task, username=username)
        items.append(s)

    return PagedTasks(total=total, page=page, page_size=page_size, items=items)


# --------------------------------------------------------------------------- #
# Company-wide per-image records (admin) + export by person
# --------------------------------------------------------------------------- #
def _admin_image_stmt(user_id: int | None = None, status: str | None = None):
    """Shared SELECT for the per-image company view (TaskItem join task+user)."""
    stmt = (
        select(TaskItem, GenerationTask, User.username)
        .join(GenerationTask, TaskItem.task_id == GenerationTask.id)
        .join(User, TaskItem.user_id == User.id)
    )
    if user_id is not None:
        stmt = stmt.where(TaskItem.user_id == user_id)
    if status:
        stmt = stmt.where(TaskItem.status == status)
    return stmt


def _to_image_record(item: TaskItem, task: GenerationTask, username: str) -> AdminImageRecord:
    orig_urls, output = _item_urls(item)
    return AdminImageRecord(
        id=item.id,
        task_id=task.id,
        owner_username=username,
        prompt=task.prompt or "",
        model=task.model,
        mode=task.mode,
        status=item.status,
        cost_usd=item.cost_usd or 0.0,
        created_at=item.created_at,
        original_urls=orig_urls,
        output_url=output,
        error_message=item.error_message,
    )


@router.get("/images", response_model=PagedImageRecords)
async def list_admin_images(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    user_id: int | None = Query(None, description="按人员(用户ID)筛选；留空=全公司"),
    status: str | None = Query(None, description="按图片状态筛选 pending|processing|completed|failed"),
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> PagedImageRecords:
    """逐张查看全公司所有生成图片记录（含提交人），支持按人员/状态过滤与分页。"""
    count_stmt = (
        select(func.count(TaskItem.id))
        .join(GenerationTask, TaskItem.task_id == GenerationTask.id)
        .join(User, TaskItem.user_id == User.id)
    )
    if user_id is not None:
        count_stmt = count_stmt.where(TaskItem.user_id == user_id)
    if status:
        count_stmt = count_stmt.where(TaskItem.status == status)
    total = (await session.execute(count_stmt)).scalar() or 0

    rows = (
        await session.execute(
            _admin_image_stmt(user_id, status)
            .order_by(desc(TaskItem.created_at))
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()

    items = [_to_image_record(item, task, username) for item, task, username in rows]
    return PagedImageRecords(total=total, page=page, page_size=page_size, items=items)


@router.get("/images/export")
async def export_admin_images(
    user_id: int | None = Query(None, description="按人员(用户ID)筛选；留空=全公司"),
    format: str = Query("csv", description="csv=图片记录报表；zip=按人员打包原图+结果图"),
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """按人员导出全公司图片记录：csv 为记录报表，zip 为按人员分组的图片包。"""
    stmt = _admin_image_stmt(user_id, None).order_by(desc(TaskItem.created_at))
    rows = (await session.execute(stmt)).all()

    if format == "zip":
        return _export_images_zip(rows)
    return _export_images_csv(rows)


def _safe_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in (name or "unknown"))


def _export_images_csv(rows) -> StreamingResponse:
    buf = io.StringIO()
    buf.write("\ufeff")  # Excel 友好 BOM
    writer = csv.writer(buf)
    writer.writerow([
        "提交人", "任务ID", "图片ID", "提示词", "模型", "模式", "状态",
        "消耗(USD)", "生成时间", "原图URL", "结果图URL",
    ])
    for item, task, username in rows:
        orig_urls, output = _item_urls(item)
        writer.writerow([
            username,
            task.id,
            item.id,
            (task.prompt or "").replace("\n", " "),
            task.model or "",
            task.mode or "single",
            item.status,
            f"{item.cost_usd or 0.0:.6f}",
            item.created_at.isoformat() if item.created_at else "",
            " | ".join(orig_urls),
            output or "",
        ])
    data = buf.getvalue().encode("utf-8-sig")
    fname = f"company_images_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    return StreamingResponse(
        io.BytesIO(data),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )


def _export_images_zip(rows) -> StreamingResponse:
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as zf:
        for item, task, username in rows:
            folder = _safe_name(username)
            stem = f"{task.id[:8]}_{item.id[:8]}"
            # 原图（多角度可能多张）
            if item.original_paths:
                try:
                    src_paths = json.loads(item.original_paths)
                except Exception:
                    src_paths = [item.original_path] if item.original_path else []
            else:
                src_paths = [item.original_path] if item.original_path else []
            for idx, rel in enumerate(src_paths):
                if not rel:
                    continue
                abs_p = os.path.join(settings.storage_dir, rel)
                if os.path.exists(abs_p):
                    ext = os.path.splitext(rel)[1] or ".png"
                    zf.write(abs_p, f"{folder}/{stem}_orig{idx+1}{ext}")
            # 结果图
            if item.output_path:
                abs_o = os.path.join(settings.storage_dir, item.output_path)
                if os.path.exists(abs_o):
                    ext = os.path.splitext(item.output_path)[1] or ".png"
                    zf.write(abs_o, f"{folder}/{stem}_out{ext}")
    mem.seek(0)
    fname = f"company_images_by_person_{datetime.now().strftime('%Y%m%d_%H%M')}.zip"
    return StreamingResponse(
        mem,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )
