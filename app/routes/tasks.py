"""Task routes: create, detail, history, zip download, and lifecycle deletion.

All routes require a valid JWT. Users may only access / delete their own
tasks; admins may access / delete any task (enforced in ``_assert_owner_or_admin``).
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import zipfile

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import worker
from app import pool as keypool
from app import settings_store
from app import storage_maintenance
from app.config import settings
from app.db import get_session
from app.deps import get_current_user, require_admin
from app.pricing import get_price
from app.prompts import assemble_prompt
from app.scenes import is_valid_theme
from app.models import (
    GenerationTask,
    HistoryTask,
    ItemOut,
    PagedTasks,
    TaskDetail,
    TaskItem,
    TaskSummary,
    User,
)
from app.worker import queue_info_for

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9_.\\-]")


def _secure_filename(name: str) -> str:
    name = os.path.basename(name)
    name = _SAFE_FILENAME.sub("_", name)
    return name or "file.bin"


def _storage_url(relative_path: str | None) -> str | None:
    return f"/storage/{relative_path}" if relative_path else None


def _remove_file(relative_path: str | None) -> None:
    if not relative_path:
        return
    path = os.path.join(settings.storage_dir, relative_path)
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _assert_owner_or_admin(task: GenerationTask, user: User) -> None:
    if user.role != "admin" and task.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not allowed to access this task.")


def _resolve_ratio_from_upload(ratio: str | None, upload: UploadFile) -> str | None:
    """「原图比例」解析：ratio 为空时读首张上传图真实宽高，映射到 Agnes 白名单最近档。

    背景：Agnes 的 ratio 参数缺省是 1:1（官方文档 Default is 1:1），空值若直接
    不发送 = 1:1 方形画布重绘，任何非方形产品都会被拉伸/压缩（忽胖忽瘦）。这里
    把「原图比例」解析成输入图比例对应的白名单档位并落库 / 注入 prompt，让画布
    比例与产品原图比例一致，从几何上消除变形。解析失败返回 None（保持原语义）。
    """
    if (ratio or "").strip():
        return (ratio or "").strip()
    try:
        from PIL import Image

        from app.presets import nearest_ratio

        upload.file.seek(0)
        with Image.open(upload.file) as im:
            w, h = im.size
        upload.file.seek(0)  # 复位，保证后续写盘 read() 从头开始
        return nearest_ratio("", w, h) or None
    except Exception:  # noqa: BLE001 —— 解析失败不阻断建任务，worker 层另有兜底
        try:
            upload.file.seek(0)
        except Exception:
            pass
        return None


async def _build_item_outs(session, task_id: str) -> list:
    """Build ItemOut list for a task, including multi-angle original_urls."""
    items = (
        await session.execute(
            select(TaskItem)
            .where(TaskItem.task_id == task_id)
            .order_by(TaskItem.created_at)
        )
    ).scalars().all()
    outs = []
    for i in items:
        orig_url = _storage_url(i.original_path)
        if i.original_paths:
            try:
                urls = [_storage_url(p) for p in json.loads(i.original_paths)]
            except Exception:
                urls = [orig_url] if orig_url else []
        else:
            urls = [orig_url] if orig_url else []
        outs.append(
            ItemOut(
                id=i.id,
                original_filename=i.original_filename,
                original_url=orig_url,
                original_urls=urls,
                output_url=_storage_url(i.output_path),
                status=i.status,
                cost_usd=i.cost_usd or 0.0,
                error_message=i.error_message,
            )
        )
    return outs


def _summary(task: GenerationTask, username: str | None = None) -> TaskSummary:
    done = task.completed_count + task.failed_count
    progress = int(round((done / task.total_count) * 100)) if task.total_count else 0
    ahead, is_cur = queue_info_for(task.id)
    message = None
    if task.status == "pending" and ahead > 0:
        message = "前置任务尚在执行中，请稍候。"
    elif is_cur:
        message = "正在处理您的任务..."
    return TaskSummary(
        id=task.id,
        prompt=task.prompt,
        full_prompt=task.full_prompt,
        status=task.status,
        total_count=task.total_count,
        completed_count=task.completed_count,
        failed_count=task.failed_count,
        progress_percent=progress,
        created_at=task.created_at,
        tasks_ahead=ahead,
        message=message,
        username=username,
        model=task.model,
        env=task.env,
        is_white_bg=bool(task.is_white_bg),
        mode=task.mode,
        ratio=task.ratio,
        cost_usd=task.cost_usd or 0.0,
    )


async def _history_task(session, task: GenerationTask) -> "HistoryTask":
    """Build the history card with its items inlined (so images render after refresh)."""
    done = task.completed_count + task.failed_count
    progress = int(round((done / task.total_count) * 100)) if task.total_count else 0
    return HistoryTask(
        id=task.id,
        prompt=task.prompt,
        full_prompt=task.full_prompt,
        status=task.status,
        total_count=task.total_count,
        completed_count=task.completed_count,
        failed_count=task.failed_count,
        progress_percent=progress,
        created_at=task.created_at,
        model=task.model,
        is_white_bg=bool(task.is_white_bg),
        mode=task.mode,
        ratio=task.ratio,
        cost_usd=task.cost_usd or 0.0,
        items=await _build_item_outs(session, task.id),
    )


# --------------------------------------------------------------------------- #
# Create
# --------------------------------------------------------------------------- #
@router.post("/create", response_model=TaskSummary)
async def create_task(
    prompt: str = Form(..., description="统一场景生成提示词"),
    files: list[UploadFile] = File(..., description="白底产品图 / 多角度参考图"),
    model: str = Form(None, description="生图模型，默认跟随后台配置"),
    env: str = Form(None, description="环境分类 indoor|outdoor|None，生成时追加对应环境提示词"),
    is_white_bg: bool = Form(False, description="纯白底模式：切换 Layer2/Layer4，避免多余杂色背景"),
    mode: str = Form(None, description="生成模式：single 单图批量 / multi_angle_fusion 多角度合成"),
    ratio: str = Form(None, description="输出比例（agnes 等支持 ratio 的模型使用，如 4:3 / 16:9；空=原图比例）"),
    theme: str = Form(None, description="节日/季节主题：ghost|spring|autumn；非空时注入居中构图锁与主题氛围层"),
    theme_random: bool = Form(False, description="主题随机变体：True=逐张随机组合背景/元素/光影（seed 可复现）"),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> TaskSummary:
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded.")

    # ---- 磁盘容量闸门：占用已达上限时拒绝新上传，避免打满磁盘 ----
    # 注意：storage_has_room() 第二个返回值是「存储占用比例(0.0~1.0)」，务必用独立
    # 变量名 storage_ratio，绝不能覆盖上面的表单参数 ratio（比例字符串），否则会把
    # 浮点数(如 0.000565)写进 GenerationTask.ratio，导致 Agnes 收到非法 ratio 而 400。
    has_room, storage_ratio = storage_maintenance.storage_has_room()
    if not has_room:
        raise HTTPException(
            status_code=507,
            detail=(
                f"服务器图片存储已满（{storage_ratio:.0%}），暂时无法创建新任务。"
                "请联系管理员清理历史任务或扩充磁盘后再试。"
            ),
        )

    # ---- Gemini 门禁：默认隐藏，管理员开启后才能使用 ----
    chosen_model = (model or "").strip() or settings_store.get_model() or ""
    price = get_price(chosen_model)
    if (
        price is not None
        and price.provider == "gemini"
        and not settings_store.get_enable_gemini()
    ):
        raise HTTPException(
            status_code=400,
            detail="Gemini 模型当前未启用（管理员在后台「API 配置」开启后才可使用）。请改用 Agnes 模型。",
        )

    # ---- 每日免费额度预检（未绑定有效 Agnes Key 的员工）----
    # 权威判定仍在 worker 内：预检只用于尽早提示，避免排队后才失败。
    is_fusion = (mode == "multi_angle_fusion")
    if user.role != "admin":
        own_key = await keypool.own_valid_key(session, user.id)
        if own_key is None:
            limit = settings_store.get_free_daily_limit()
            used = await keypool.used_today(session, user.id)
            will_generate = 1 if is_fusion else len(files)
            if used + will_generate > limit:
                remain = max(0, limit - used)
                raise HTTPException(
                    status_code=429,
                    detail=(
                        f"今日免费额度已用完（{limit} 张/天，已用 {used} 张）。"
                        + (
                            f"本次需 {will_generate} 张，还差 {will_generate - remain} 张。"
                            if remain < will_generate and remain > 0
                            else ""
                        )
                        + "请在右上角「🔑 API Key」绑定自己的 Agnes Key 解锁不限量，或明日 0 点后再试。"
                    ),
                )

    if is_fusion and not (2 <= len(files) <= 4):
        raise HTTPException(
            status_code=400,
            detail="多角度合成模式需要上传 2~4 张同一产品的不同角度图。",
        )

    if env not in (None, "indoor", "outdoor"):
        env = None

    # 主题（鬼节 / 春 / 秋）：非法值直接忽略，退化为普通场景，不影响主流程。
    theme = (theme or "").strip() or None
    if theme and not is_valid_theme(theme):
        theme = None

    # 「原图比例」解析：Agnes 的 ratio 缺省是 1:1，空值必须解析成首张上传图
    # 的真实比例（白名单最近档）显式发送，否则非方形产品会被拉到方形画布变形。
    effective_ratio = _resolve_ratio_from_upload(ratio, files[0])

    # 后端在创建任务时即把用户填写的场景提示词拼装为四层结构
    # （保真锁 + 物理防畸变 + 用户场景 + 商业画质），result 存入 full_prompt，
    # 既作为实际生图用的提示词，也用于记录展示与一键复制。
    # white_bg 模式由前端显式传入（选了 A 组纯白预设即 true），否则按文本兜底识别。
    # 多角度合成模式使用专用 Layer1（多参考图理解 3D 结构并融合成单张场景）。
    # 选定主题时额外注入「居中构图锁 + 主题氛围约束」层（位于 Layer2 之后）。
    full_prompt = assemble_prompt(
        prompt, white_bg=is_white_bg, multi_angle=is_fusion, theme=theme,
        ratio=effective_ratio,
    )

    task = GenerationTask(
        user_id=user.id, prompt=prompt, full_prompt=full_prompt,
        total_count=1 if is_fusion else len(files),
        model=model or None, env=env, is_white_bg=bool(is_white_bg),
        mode="multi_angle_fusion" if is_fusion else None,
        ratio=effective_ratio or None, theme=theme, theme_random=bool(theme_random),
    )
    session.add(task)
    await session.flush()  # populate task.id (UUID)

    upload_dir = os.path.join(settings.uploads_dir, task.id)
    os.makedirs(upload_dir, exist_ok=True)

    if is_fusion:
        # 多角度合成：所有参考图落地，单个 TaskItem 关联整组原图，产出 1 张融合图。
        rel_paths = []
        for idx, upload in enumerate(files):
            filename = _secure_filename(upload.filename or f"image_{idx}")
            dest_rel = f"uploads/{task.id}/{filename}"
            dest_abs = os.path.join(settings.storage_dir, dest_rel)
            content = await upload.read()
            with open(dest_abs, "wb") as fh:
                fh.write(content)
            rel_paths.append(dest_rel)
        item = TaskItem(
            task_id=task.id,
            user_id=user.id,
            original_filename=rel_paths[0].split("/")[-1],
            original_path=rel_paths[0],
            original_paths=json.dumps(rel_paths),
        )
        session.add(item)
        await session.flush()
        item_ids = [item.id]
    else:
        item_ids = []
        for idx, upload in enumerate(files):
            filename = _secure_filename(upload.filename or f"image_{idx}")
            dest_rel = f"uploads/{task.id}/{filename}"
            dest_abs = os.path.join(settings.storage_dir, dest_rel)
            content = await upload.read()
            with open(dest_abs, "wb") as fh:
                fh.write(content)

            item = TaskItem(
                task_id=task.id,
                user_id=user.id,
                original_filename=filename,
                original_path=dest_rel,
            )
            session.add(item)
            await session.flush()
            item_ids.append(item.id)

    await session.commit()
    await session.refresh(task)

    # Hand the work to the background flow-control queue.
    worker.enqueue_task(task.id, item_ids)
    return _summary(task)


# --------------------------------------------------------------------------- #
# History (paginated) -- MUST be declared before "/{task_id}" so it is not
# shadowed by the task_id path parameter.
# --------------------------------------------------------------------------- #
@router.get("/my-history", response_model=PagedTasks)
async def my_history(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> PagedTasks:
    total = (
        await session.execute(
            select(func.count(GenerationTask.id)).where(GenerationTask.user_id == user.id)
        )
    ).scalar() or 0

    rows = (
        await session.execute(
            select(GenerationTask)
            .where(GenerationTask.user_id == user.id)
            .order_by(desc(GenerationTask.created_at))
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()

    return PagedTasks(
        total=total,
        page=page,
        page_size=page_size,
        items=[await _history_task(session, t) for t in rows],
    )


# --------------------------------------------------------------------------- #
# Detail
# --------------------------------------------------------------------------- #
@router.get("/{task_id}", response_model=TaskDetail)
async def get_task(
    task_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> TaskDetail:
    task = await session.get(GenerationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    _assert_owner_or_admin(task, user)

    items = (
        await session.execute(
            select(TaskItem).where(TaskItem.task_id == task_id)
            .order_by(TaskItem.created_at)
        )
    ).scalars().all()

    summary = _summary(task)
    item_outs = await _build_item_outs(session, task.id)
    return TaskDetail(
        id=task.id,
        prompt=task.prompt,
        full_prompt=task.full_prompt,
        status=task.status,
        total_count=task.total_count,
        completed_count=task.completed_count,
        failed_count=task.failed_count,
        progress_percent=summary.progress_percent,
        created_at=task.created_at,
        items=item_outs,
        tasks_ahead=summary.tasks_ahead,
        message=summary.message,
        model=task.model,
        env=task.env,
        is_white_bg=bool(task.is_white_bg),
        mode=task.mode,
        ratio=task.ratio,
        cost_usd=task.cost_usd or 0.0,
    )


# --------------------------------------------------------------------------- #
# Zip download
# --------------------------------------------------------------------------- #
@router.get("/{task_id}/download-zip")
async def download_zip(
    task_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    task = await session.get(GenerationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    _assert_owner_or_admin(task, user)

    items = (
        await session.execute(
            select(TaskItem).where(
                TaskItem.task_id == task_id, TaskItem.status == "success"
            )
        )
    ).scalars().all()
    if not items:
        raise HTTPException(status_code=404, detail="No generated images available yet.")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in items:
            abs_path = os.path.join(settings.storage_dir, item.output_path)
            zf.write(abs_path, arcname=f"{item.id}_{item.original_filename}")
    buffer.seek(0)

    def _iter() -> bytes:
        buffer.seek(0)
        while chunk := buffer.read(65536):
            yield chunk

    headers = {"Content-Disposition": f'attachment; filename="scene_{task_id}.zip"'}
    return StreamingResponse(_iter(), media_type="application/zip", headers=headers)


# --------------------------------------------------------------------------- #
# Lifecycle deletion
# --------------------------------------------------------------------------- #
@router.delete("/items/{item_id}")
async def delete_item(
    item_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    item = await session.get(TaskItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found.")
    if user.role != "admin" and item.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not allowed to delete this item.")

    task = await session.get(GenerationTask, item.task_id)
    owner = await session.get(User, item.user_id)
    # 回滚该图片产生的消耗（任务级 + 用户累计）。
    cost = item.cost_usd or 0.0
    # Physical cleanup of both source and generated image.
    _remove_file(item.original_path)
    _remove_file(item.output_path)
    # 多角度合成：删除整组参考原图。
    if item.original_paths:
        try:
            for p in json.loads(item.original_paths):
                _remove_file(p)
        except Exception:
            pass
    await session.delete(item)
    await session.flush()

    if task is not None:
        if owner is not None:
            owner.total_cost_usd = max(0.0, (owner.total_cost_usd or 0.0) - cost)
        await _recompute_task(session, task)
    await session.commit()
    return {"ok": True, "deleted_item_id": item_id}


@router.delete("/{task_id}")
async def delete_task(
    task_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    task = await session.get(GenerationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    _assert_owner_or_admin(task, user)

    items = (
        await session.execute(select(TaskItem).where(TaskItem.task_id == task_id))
    ).scalars().all()
    for item in items:
        _remove_file(item.original_path)
        _remove_file(item.output_path)
        await session.delete(item)

    # 回滚整个任务的累计消耗（任务级 + 用户累计）。
    if task.cost_usd:
        owner = await session.get(User, task.user_id)
        if owner is not None:
            owner.total_cost_usd = max(0.0, (owner.total_cost_usd or 0.0) - task.cost_usd)

    await session.delete(task)
    await session.commit()

    # Remove now-empty task directories.
    shutil.rmtree(os.path.join(settings.uploads_dir, task_id), ignore_errors=True)
    shutil.rmtree(os.path.join(settings.outputs_dir, task_id), ignore_errors=True)
    # Drop from queue ordering if present.
    worker._cleanup_order(task_id)

    return {"ok": True, "deleted_task_id": task_id}


async def _recompute_task(session: AsyncSession, task: GenerationTask) -> None:
    """Recount item statuses and cost for a task after an item deletion."""
    items = (
        await session.execute(select(TaskItem).where(TaskItem.task_id == task.id))
    ).scalars().all()
    task.total_count = len(items)
    task.completed_count = sum(1 for i in items if i.status == "success")
    task.failed_count = sum(1 for i in items if i.status == "failed")
    task.cost_usd = round(sum((i.cost_usd or 0.0) for i in items), 6)
    done = task.completed_count + task.failed_count
    if task.total_count > 0 and done >= task.total_count:
        task.status = "failed" if task.completed_count == 0 else "completed"
    else:
        task.status = "processing"
