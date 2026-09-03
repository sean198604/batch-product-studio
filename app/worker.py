"""Background queue + worker + global rate limiting (DB-backed).

Design
------
* A single :class:`asyncio.Queue` holds work units = ``item_id`` (UUID string).
* ONE worker coroutine drains the queue sequentially -> never more than one
  in-flight Gemini call (satisfies Semaphore(1)).
* After *every successful* generation we ``asyncio.sleep(3.5s)`` to keep the
  real request rate well under the Free Tier ~10 RPM cap.
* On HTTP 429 / 5xx / network errors the Gemini client retries with
  5/10/20s backoff; if exhausted the single image is marked ``failed`` and the
  next image is processed (a failure never blocks the queue).
* After each success we atomically bump the owning User's
  ``total_api_calls`` and ``total_images_generated`` inside the same commit.
* ``_task_order`` records the FIFO submission order of tasks that still have
  pending items, powering the "前置任务尚在执行中" hint on the frontend.
* On startup ``recover_pending`` re-enqueues any items left in
  pending/processing from a previous run (DB is the source of truth).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import tempfile
from typing import List, Optional

from sqlalchemy import select

from app import models
from app.config import settings
from app import settings_store
from app import pool as keypool
from app.db import async_session_maker
from app.gemini import GeminiClient, mime_to_ext
from app.agnes import AgnesClient, looks_like_auth_error
from app.prompts import assemble_prompt
from app.scenes import render_theme_prompt
from app import pricing

logger = logging.getLogger("worker")

# ---- runtime globals (populated in start_worker) ----
_queue: Optional[asyncio.Queue] = None
_semaphore: Optional[asyncio.Semaphore] = None
_client: Optional[GeminiClient] = None
_agnes_client: Optional[AgnesClient] = None
_task: Optional[asyncio.Task] = None

# FIFO order of task_ids that still have pending items.
_task_order: List[str] = []
# The task_id currently being processed (None when idle).
_current_task: Optional[str] = None


def _abs(path_relative: str) -> str:
    """Resolve a storage-relative path to an absolute filesystem path."""
    return os.path.join(settings.storage_dir, path_relative)


def _prepare_api_image(original_path: str) -> str:
    """Return a path to a clean, standard PNG fit for the Gemini image API.

    Some uploads trigger Google's cryptic ``Unable to process input image``
    400: oddly-encoded JPEGs, wrong/missing file extensions, or exotic formats.
    Re-encoding the file with Pillow to an RGB PNG standardises the bytes so
    the model can always decode it. If Pillow is unavailable we fall back to
    the original path; if the file is not a decodable image we raise a clear,
    actionable error instead of letting Google return a confusing 400.
    """
    try:
        from PIL import Image
    except Exception:
        return original_path
    try:
        with Image.open(original_path) as im:
            im = im.convert("RGB")  # drop alpha, standardise to 3 channels
            fd, tmp = tempfile.mkstemp(suffix=".png")
            os.close(fd)
            im.save(tmp, "PNG")
            return tmp
    except Exception as exc:
        raise RuntimeError(
            f"无法读取/处理该上传图片，请上传标准 JPG/PNG/WEBP（解码失败：{exc}）"
        ) from exc


def _build_prompt(task, multi: bool) -> str:
    """Resolve the prompt to send for one item, honouring the theme mode.

    Three cases:
    * no theme            -> reuse ``task.full_prompt`` (assembled at creation).
    * theme, not random   -> reuse ``task.full_prompt`` too: the staff picked a
                             curated chip, so what they saw in the box (plus the
                             injected centering / atmosphere layers) is what we
                             send. Keeps the console WYSIWYG.
    * theme + random      -> compose a FRESH combination per item from the theme
                             vocabulary with a random seed, so a batch of N
                             products yields N distinct on-theme scenes. The
                             staff's own text (if any) is preserved as an extra
                             requirement rather than overwritten.
    """
    theme = (getattr(task, "theme", None) or "").strip() or None
    if not theme:
        return task.full_prompt or assemble_prompt(task.prompt, multi_angle=multi)

    if not getattr(task, "theme_random", False):
        return task.full_prompt or assemble_prompt(
            task.prompt, multi_angle=multi, theme=theme
        )

    theme_text, meta = render_theme_prompt(theme, seed=random.randint(100000, 999999))
    extra = (task.prompt or "").strip()
    body = f"{theme_text} Additional requirements: {extra}" if extra else theme_text
    logger.info(
        "theme variant: %s seed=%s bg=%s el=%s light=%s",
        meta["theme"], meta["seed"], meta["background"],
        meta["element"], meta["light"],
    )
    return assemble_prompt(body, multi_angle=multi, theme=theme)


async def start_worker() -> None:
    global _queue, _semaphore, _client, _agnes_client, _task
    _queue = asyncio.Queue()
    _semaphore = asyncio.Semaphore(settings.semaphore_concurrency)
    _client = GeminiClient()
    _agnes_client = AgnesClient()
    await recover_pending()
    _task = asyncio.create_task(_run(), name="gemini-worker")
    logger.info("Background worker started (concurrency=%d, interval=%.1fs).",
                settings.semaphore_concurrency, settings.request_interval_seconds)


async def stop_worker() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None


async def recover_pending() -> None:
    """Re-enqueue DB items left pending/processing (e.g. after a restart)."""
    _task_order.clear()
    async with async_session_maker() as session:
        tasks = (
            await session.execute(
                select(models.GenerationTask)
                .where(models.GenerationTask.status.in_(["pending", "processing"]))
                .order_by(models.GenerationTask.created_at)
            )
        ).scalars().all()

        for task in tasks:
            _task_order.append(task.id)
            items = (
                await session.execute(
                    select(models.TaskItem)
                    .where(
                        models.TaskItem.task_id == task.id,
                        models.TaskItem.status.in_(["pending", "processing"]),
                    )
                    .order_by(models.TaskItem.created_at)
                )
            ).scalars().all()
            for item in items:
                item.status = "pending"  # reset interrupted "processing"
                await _queue.put(item.id)
            # Recompute counts in case some items were already done.
            await _recompute_task_counts(session, task)
        await session.commit()
    logger.info("Recovered %d pending task(s) into the queue.", len(_task_order))


def enqueue_task(task_id: str, item_ids: List[str]) -> None:
    """Register a task in FIFO order and push its items onto the queue."""
    if task_id not in _task_order:
        _task_order.append(task_id)
    for iid in item_ids:
        _queue.put_nowait(iid)


def queue_info_for(task_id: str) -> tuple[int, bool]:
    """Return (tasks_ahead, is_currently_processing) for a given task."""
    ahead = 0
    for tid in _task_order:
        if tid == task_id:
            break
        ahead += 1
    return ahead, _current_task == task_id


async def _run() -> None:
    assert _queue is not None
    while True:
        try:
            item_id = await _queue.get()
        except asyncio.CancelledError:
            break
        try:
            await _process(item_id)
        except Exception:  # never let the worker die on one bad item
            logger.exception("Unexpected error processing item %s", item_id)
        finally:
            _queue.task_done()


async def _process(item_id: str) -> None:
    """Process a single image: call Gemini, persist output, update DB."""
    global _current_task
    async with async_session_maker() as session:
        item = await session.get(models.TaskItem, item_id)
        if item is None or item.status in ("success", "deleted"):
            return
        task = await session.get(models.GenerationTask, item.task_id)
        if task is None:
            return
        user = await session.get(models.User, item.user_id)

        item.status = "processing"
        task.status = "processing"
        _current_task = task.id

        # Agnes 池中实际扣减的 Key（成功则记 last_used_at；401/403 则标失效）
        used_key = None

        try:
            async with _semaphore:
                # ---- 每日免费额度闸门（未绑定有效 Agnes Key 的员工适用）----
                # 管理员与已绑定有效 Key 的用户不限量；其余用户每天最多
                # free_daily_limit 张（北京日历日）。这里的判定是权威的：
                # 创建任务时的预检只是提前拦截，此处保证队列堆积也不超限。
                if user is not None and not await keypool.has_unlimited(session, user):
                    limit = settings_store.get_free_daily_limit()
                    if await keypool.used_today(session, user.id) >= limit:
                        raise RuntimeError(
                            f"今日免费额度（{limit} 张/天）已用完。"
                            "请在右上角「🔑 API Key」绑定自己的 Agnes Key 解锁不限量，"
                            "或明日 0 点后再试。"
                        )

                # The backend "物理真实感与防畸变拼装引擎" wraps the user prompt
                # into a strict 4-layer structure (保真锁 + 防畸变 + 用户场景 +
                # 商业画质) at task creation time; the result is stored in
                # task.full_prompt and reused for every image in the task. A
                # per-task model override is honoured here. The upload is
                # re-encoded to a clean PNG first so a malformed / oddly-encoded
                # source can't trigger Google's "Unable to process input image".
                multi = (task.mode == "multi_angle_fusion")
                use_agnes = bool(task.model) and task.model.startswith("agnes-")
                if use_agnes:
                    # Agnes 图生图：单图与多角度合成都合并进 extra_body.image。
                    # 解析本次生成使用的池 Key：自有（不限量）-> 系统 Key -> 池内轮换。
                    uid = user.id if user is not None else 0
                    used_key = await keypool.pick_pool_key(session, uid)
                    agnes_api_key = (
                        used_key.key_value
                        if used_key is not None
                        else settings_store.get_agnes_key()
                    )
                    if multi:
                        src_paths = (
                            json.loads(item.original_paths)
                            if item.original_paths
                            else [item.original_path]
                        )
                    else:
                        src_paths = [item.original_path]
                    api_images = [_prepare_api_image(_abs(p)) for p in src_paths]
                    prompt_to_send = _build_prompt(task, multi)
                    try:
                        image_bytes, mime = await _agnes_client.generate(
                            prompt_to_send,
                            api_images,
                            model=task.model,
                            size=settings_store.get_agnes_size_tier(),
                            ratio=task.ratio or "",
                            api_key=agnes_api_key,
                        )
                    finally:
                        for ai in api_images:
                            if ai != _abs(item.original_path) and os.path.exists(ai):
                                try:
                                    os.remove(ai)
                                except OSError:
                                    pass
                elif multi:
                    # 多角度合成：把多张参考图作为多个 inline_data 一次性提交，
                    # 由模型融合成单张场景图。
                    import json as _json  # noqa: F401
                    src_paths = (
                        json.loads(item.original_paths)
                        if item.original_paths
                        else [item.original_path]
                    )
                    api_images = [_prepare_api_image(_abs(p)) for p in src_paths]
                    prompt_to_send = _build_prompt(task, multi)
                    try:
                        image_bytes, mime = await _client.generate_image_multi(
                            prompt_to_send, api_images, model=task.model
                        )
                    finally:
                        for ai in api_images:
                            if ai != _abs(item.original_path) and os.path.exists(ai):
                                try:
                                    os.remove(ai)
                                except OSError:
                                    pass
                else:
                    api_image = _prepare_api_image(_abs(item.original_path))
                    prompt_to_send = _build_prompt(task, multi)
                    try:
                        image_bytes, mime = await _client.generate_image(
                            prompt_to_send, api_image, model=task.model
                        )
                    finally:
                        if api_image != _abs(item.original_path) and os.path.exists(api_image):
                            try:
                                os.remove(api_image)
                            except OSError:
                                pass

            out_rel = f"outputs/{task.id}/scene_{item.id}.{mime_to_ext(mime)}"
            out_abs = _abs(out_rel)
            os.makedirs(os.path.dirname(out_abs), exist_ok=True)
            with open(out_abs, "wb") as fh:
                fh.write(image_bytes)

            item.output_path = out_rel
            item.status = "success"
            item.error_message = None
            # 图生图单价：按本任务所用模型计算单张成本（含 1 输入图 + 1 输出图）。
            cost = pricing.compute_cost_usd(task.model)
            item.cost_usd = cost
            task.completed_count += 1
            task.cost_usd = (task.cost_usd or 0.0) + cost
            if user is not None:
                user.total_api_calls += 1          # atomic within this commit
                user.total_images_generated += 1
                user.total_cost_usd = (user.total_cost_usd or 0.0) + cost
            if used_key is not None:
                await keypool.mark_key_used(session, used_key)

            # ---- global pacing: enforce safe interval AFTER a success ----
            await session.commit()
            await asyncio.sleep(settings.request_interval_seconds)

        except Exception as exc:
            item.status = "failed"
            item.error_message = str(exc)
            task.failed_count += 1
            # 服务端明确拒绝该 Key（401/403）→ 自动移出可用池，避免反复打到坏 Key。
            if used_key is not None and looks_like_auth_error(str(exc)):
                await keypool.mark_key_invalid(
                    session, used_key, f"生图被服务端拒绝，已自动失效：{str(exc)[:200]}"
                )
            await session.commit()
            logger.error("Item %s failed: %s", item_id, exc)
        finally:
            _current_task = None
            _update_task_status(task)
            await session.commit()
            # Only drop the task from the FIFO order once it is fully done.
            if task.status in ("completed", "failed"):
                _cleanup_order(task.id)


def _update_task_status(task: models.GenerationTask) -> None:
    done = task.completed_count + task.failed_count
    if task.total_count > 0 and done >= task.total_count:
        task.status = "failed" if task.completed_count == 0 else "completed"
    else:
        task.status = "processing"


async def _recompute_task_counts(session, task: models.GenerationTask) -> None:
    """Recount item statuses and cost into the task's aggregate fields."""
    items = (
        await session.execute(
            select(models.TaskItem).where(models.TaskItem.task_id == task.id)
        )
    ).scalars().all()
    task.total_count = len(items)
    task.completed_count = sum(1 for i in items if i.status == "success")
    task.failed_count = sum(1 for i in items if i.status == "failed")
    task.cost_usd = round(sum((i.cost_usd or 0.0) for i in items), 6)
    _update_task_status(task)


def _cleanup_order(task_id: str) -> None:
    """Drop a finished task from the FIFO order (idempotent)."""
    if task_id in _task_order:
        _task_order.remove(task_id)
