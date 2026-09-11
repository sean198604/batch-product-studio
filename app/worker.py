"""Background queue: multi-task parallel pool + strict-FIFO back-wait queue.

Design
-------
* Up to ``settings.max_concurrent_tasks`` tasks run in parallel; each task
  still processes its images **sequentially** (one in-flight Gemini call at a
  time) so the per-task rate-limit pacing (``request_interval_seconds``) is
  preserved and we never exceed the Free Tier ~10 RPM cap.
* Tasks beyond the parallel cap go to a strict-FIFO waiting queue and are
  promoted to active the moment a slot frees up. New tasks always jump to the
  back of the line — no "submission order within an active slot" wishful
  thinking.
* Per-task item queues are kept in-memory; the DB is the source of truth on
  recovery.
* On HTTP 429 / 5xx / network errors the Gemini client retries with
  5/10/20s backoff; if exhausted the single image is marked ``failed`` and the
  next image is processed (a failure never blocks the queue).
* After each success we ``asyncio.sleep(request_interval_seconds)`` to keep the
  real request rate well under the Free Tier ~10 RPM cap, then atomically bump
  the owning User's ``total_api_calls`` / ``total_images_generated`` /
  ``total_cost_usd`` inside the same commit.
* ``recover_pending`` re-enqueues any items left in pending/processing from a
  previous run through the new dispatcher; the DB record is the source of
  truth so restart does not lose work.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import tempfile
from collections import OrderedDict, deque
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

# --------------------------------------------------------------------------- #
# Runtime state
# --------------------------------------------------------------------------- #
# Active task slots (FIFO, capacity = ``_max_concurrent``). Each entry is a
# task_id and we O(1)-pop oldest when promoting a waiter.
_active_task_ids: "OrderedDict[str, None]" = OrderedDict()
# Back-wait queue (strict FIFO; popped from the left when a slot frees).
_waiting_task_ids: "deque[str]" = deque()
# Per-task FIFO of item_ids waiting to be processed in-memory.
_item_queues: "dict[str, deque[str]]" = {}
# The task_id currently being processed by an **individual** worker slot.
# (Used by ``queue_info_for`` to render "正在处理您的任务…".)
_current_task: Optional[str] = None

# Worker handles (created in ``start_worker``).
_worker_tasks: List[asyncio.Task] = []
# Wake-up signal: workers sleep here when there's no immediate work.
_wakeup: Optional[asyncio.Event] = None

# Clients (initialised in ``start_worker``).
_client: Optional[GeminiClient] = None
_agnes_cn_client: Optional[AgnesClient] = None   # 国内站（agnes-ai.cn）
_agnes_intl_client: Optional[AgnesClient] = None  # 国际站（agnes-ai.com）
_semaphore: Optional[asyncio.Semaphore] = None  # global single in-flight channel

# Cached at start_worker to avoid re-reading settings.
_max_concurrent: int = 1


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
    """Bootstrap the parallel-pool worker fan-out."""
    global _wakeup, _client, _agnes_client, _semaphore, _max_concurrent
    _wakeup = asyncio.Event()
    _client = GeminiClient()
    _agnes_cn_client = AgnesClient(station="cn")
    _agnes_intl_client = AgnesClient(station="intl")
    _semaphore = asyncio.Semaphore(settings.semaphore_concurrency)
    _max_concurrent = max(1, int(settings.max_concurrent_tasks))

    await recover_pending()

    for i in range(_max_concurrent):
        w = asyncio.create_task(_worker_loop(i), name=f"gemini-worker-{i}")
        _worker_tasks.append(w)

    logger.info(
        "Background pool started (parallel_tasks=%d, interval=%.1fs).",
        _max_concurrent, settings.request_interval_seconds,
    )


async def stop_worker() -> None:
    """Cancel all worker tasks on shutdown (idempotent)."""
    global _worker_tasks
    for t in _worker_tasks:
        t.cancel()
    for t in _worker_tasks:
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass
    _worker_tasks = []


async def recover_pending() -> None:
    """Re-enqueue DB items left pending/processing (e.g. after a restart).

    Honours the parallel cap exactly like :func:`enqueue_task`: tasks beyond
    the cap land in the waiting queue and get promoted when slots free up.
    """
    async with async_session_maker() as session:
        tasks = (
            await session.execute(
                select(models.GenerationTask)
                .where(models.GenerationTask.status.in_(["pending", "processing"]))
                .order_by(models.GenerationTask.created_at)
            )
        ).scalars().all()

        for task in tasks:
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
            item_ids: List[str] = []
            for item in items:
                item.status = "pending"  # reset interrupted "processing"
                item_ids.append(item.id)
            await _recompute_task_counts(session, task)
            if item_ids:
                _accept_task(task.id, item_ids)
        await session.commit()
    logger.info(
        "Recovered %d pending task(s) (active=%d, waiting=%d).",
        len(_active_task_ids) + len(_waiting_task_ids),
        len(_active_task_ids),
        len(_waiting_task_ids),
    )


def enqueue_task(task_id: str, item_ids: List[str]) -> None:
    """Register a task in FIFO order and push its items through the dispatcher."""
    if not item_ids:
        return
    _accept_task(task_id, list(item_ids))
    if _wakeup is not None:
        _wakeup.set()


def _accept_task(task_id: str, item_ids: List[str]) -> None:
    """Place a task's items in memory and assign to active slot or waiter queue.

    Idempotent against being called multiple times for the same task (e.g.
    recover_pending touches tasks that may have been enqueued live too).
    """
    existing = _item_queues.get(task_id)
    if existing is None:
        _item_queues[task_id] = deque(item_ids)
    else:
        existing.extend(item_ids)
    # If already active or waiting, no need to re-queue.
    if task_id in _active_task_ids or task_id in _waiting_task_ids:
        return
    if len(_active_task_ids) < _max_concurrent:
        _active_task_ids[task_id] = None  # take a slot
    else:
        _waiting_task_ids.append(task_id)


def queue_info_for(task_id: str) -> tuple[int, bool]:
    """Return (tasks_ahead, is_currently_processing) for a given task.

    * ``tasks_ahead``  - tasks strictly ahead of this one in the global FIFO
      order (active slots first, then waiting queue). Mirrors the old
      behaviour so the "前置任务尚在执行中" frontend hint keeps working.
    * ``is_currently_processing`` - True iff this task is the one a worker
      picked up this moment.
    """
    for pos, tid in enumerate(_active_task_ids.keys()):
        if tid == task_id:
            return pos, (_current_task == task_id)
    for pos, tid in enumerate(_waiting_task_ids):
        if tid == task_id:
            return len(_active_task_ids) + pos, False
    # Task is neither active nor waiting (finished, recovered later, etc.).
    return 0, False


def _has_immediate_work() -> bool:
    """True iff at least one active task still has pending in-memory items."""
    for tid in _active_task_ids:
        q = _item_queues.get(tid)
        if q and len(q):
            return True
    return False


def _pick_next_item() -> Optional[tuple[str, str]]:
    """Return ``(task_id, item_id)`` of the next item to process, else None.

    Walks the active set in FIFO order, returning the first task whose
    in-memory queue is non-empty. Single ``pop`` removes the item from the
    queue but the task stays in ``_active_task_ids`` until it is finalised
    (see :func:`_cleanup_order`).

    Empty deques are treated as "no work" so we never call ``popleft()`` on
    an empty queue (which would raise ``IndexError``).
    """
    for tid in list(_active_task_ids.keys()):
        q = _item_queues.get(tid)
        if q and len(q):
            return tid, q.popleft()
    return None


async def _worker_loop(worker_id: int) -> None:
    """One worker coroutine. Sleeps on ``_wakeup`` when idle."""
    assert _wakeup is not None
    while True:
        try:
            await _wakeup.wait()
        except asyncio.CancelledError:
            return
        while True:
            picked = _pick_next_item()
            if picked is None:
                # Reset wakeup so a *subsequent* task arrival re-fires us.
                _wakeup.clear()
                if _has_immediate_work():
                    # Race: another worker may have set it; ensure set.
                    _wakeup.set()
                    continue
                break
            tid, item_id = picked
            try:
                await _process(item_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "worker-%d: unexpected error processing item %s (task %s)",
                    worker_id, item_id, tid,
                )


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

        # 站点解析（在免费额度闸门之前确定，用于按站点隔离不限量判定）：
        # agnes-intl-* = 国际站（agnes-ai.com），agnes-* = 国内站（agnes-ai.cn），
        # 其余 = Gemini。国际站模型 id 在调用前剥掉 intl- 前缀还原为 agn- 原 id。
        multi = (task.mode == "multi_angle_fusion")
        eff_model = (task.model or "").strip() or settings_store.get_model() or ""
        if eff_model.startswith("agnes-intl-"):
            station = "intl"
            use_agnes = True
            api_model = eff_model[len("agnes-intl-"):]
        elif eff_model.startswith("agnes-"):
            station = "cn"
            use_agnes = True
            api_model = eff_model
        else:
            station = None
            use_agnes = False
            api_model = eff_model

        try:
            async with _semaphore:
                # ---- 每日免费额度闸门（未绑定有效 Agnes Key 的员工适用）----
                # 管理员与已绑定有效 Key 的用户不限量；其余用户每天最多
                # free_daily_limit 张（北京日历日）。按 station 隔离：国际站任务
                # 需该站点有绑定的有效 Key 才不限量。创建任务时的预检只是提前拦
                # 截，此处保证队列堆积也不超限。
                if user is not None and not await keypool.has_unlimited(session, user, station):
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
                # （station / use_agnes / api_model 已在上方免费额度闸门前解析。）
                if use_agnes:
                    # Agnes 图生图：单图与多角度合成都合并进 extra_body.image。
                    # 解析本次生成使用的池 Key：自有（不限量）-> 系统 Key -> 池内轮换。
                    # 按 station 隔离：国际站任务只用国际站 Key 池，互不串用。
                    uid = user.id if user is not None else 0
                    used_key = await keypool.pick_pool_key(session, uid, station)
                    agnes_api_key = (
                        used_key.key_value
                        if used_key is not None
                        else settings_store.get_agnes_key(station)
                    )
                    agnes_client = (
                        _agnes_intl_client if station == "intl" else _agnes_cn_client
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
                        image_bytes, mime = await agnes_client.generate(
                            prompt_to_send,
                            api_images,
                            model=api_model,
                            size=settings_store.get_agnes_size_tier(station),
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
                            prompt_to_send, api_images, model=eff_model
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
                            prompt_to_send, api_image, model=eff_model
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
            cost = pricing.compute_cost_usd(eff_model)
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
            # Only drop the task from the active set once it is fully done.
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
    """Drop a finished task from the in-memory active set and promote a waiter.

    Safe to call repeatedly: each cleanup is idempotent.

    After the active slot frees, we promote the longest-waiting task from
    ``_waiting_task_ids``. If the waiter queue is empty we simply leave the
    slot vacant — the next :func:`_accept_task` will fill it.
    """
    was_active = task_id in _active_task_ids
    _active_task_ids.pop(task_id, None)
    _item_queues.pop(task_id, None)
    if not was_active:
        return
    # Promote FIFO head of waiting into the freed active slot.
    while _waiting_task_ids and len(_active_task_ids) < _max_concurrent:
        nxt = _waiting_task_ids.popleft()
        # If the promoted waiter happens to have no items (e.g. it was a
        # recovered task whose items all turned out to already be done), skip
        # it. We loop instead of breaking so we may still promote a later
        # waiter that *does* have work.
        if _item_queues.get(nxt) and len(_item_queues[nxt]):
            _active_task_ids[nxt] = None
            if _wakeup is not None:
                _wakeup.set()
            break
        _item_queues.pop(nxt, None)


def pool_snapshot() -> dict:
    """Diagnostic snapshot of the parallel pool (for admin / debug)."""
    return {
        "max_concurrent": _max_concurrent,
        "active_task_ids": list(_active_task_ids.keys()),
        "waiting_task_ids": list(_waiting_task_ids),
        "current_task": _current_task,
    }
