"""Storage maintenance: automatic disk governance.

职责（三件事，全部幂等、可重复执行）：

1. **过期清理** —— 删除超过 ``storage_retention_days`` 天的任务（连同原图、
   生成图、DB 记录），回收磁盘。
2. **容量兜底** —— 当 ``storage`` 目录累计超过 ``storage_max_mb`` 时，从
   **最老的任务**开始继续删除，直到占用降回上限以内。
3. **占用告警** —— 每次运行后把当前占用写入 ``data/storage_alert.json``，
   供宿主机 cron 监控脚本读取后发邮件告警（超 ``storage_warn_ratio`` 即告警）。

删除语义与 ``routes/tasks.py`` 的 ``delete_task`` 保持一致：删除文件的同时
回滚任务的累计消耗（``user.total_cost_usd``），保证「累计成本」始终只统计
当前仍存在的图。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import models
from app.config import settings
from app.db import async_session_maker
from app import worker

logger = logging.getLogger("storage_maintenance")

_ALERT_PATH = os.path.join(settings.data_dir, "storage_alert.json")


def _abs(relative_path: str | None) -> str | None:
    if not relative_path:
        return None
    return os.path.join(settings.storage_dir, relative_path)


def _remove_file(relative_path: str | None) -> None:
    path = _abs(relative_path)
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def get_storage_size_bytes() -> int:
    """Recursively sum the size of everything under ``storage_dir``."""
    total = 0
    base = settings.storage_dir
    if not os.path.isdir(base):
        return 0
    for root, _dirs, files in os.walk(base):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


async def _purge_task(session, task: models.GenerationTask) -> None:
    """Delete one task's DB rows + files + directories, rolling back its cost."""
    items = (
        await session.execute(
            select(models.TaskItem).where(models.TaskItem.task_id == task.id)
        )
    ).scalars().all()

    for item in items:
        _remove_file(item.original_path)
        _remove_file(item.output_path)
        if item.original_paths:
            try:
                for p in json.loads(item.original_paths):
                    _remove_file(p)
            except Exception:
                pass
        await session.delete(item)

    if task.cost_usd:
        owner = await session.get(models.User, task.user_id)
        if owner is not None:
            owner.total_cost_usd = max(
                0.0, (owner.total_cost_usd or 0.0) - task.cost_usd
            )

    await session.delete(task)
    shutil.rmtree(os.path.join(settings.uploads_dir, task.id), ignore_errors=True)
    shutil.rmtree(os.path.join(settings.outputs_dir, task.id), ignore_errors=True)
    worker._cleanup_order(task.id)


async def _cleanup_expired(session) -> int:
    """Delete tasks older than ``storage_retention_days``. Returns count removed.

    只清理已终结（completed/failed）的任务，绝不触碰仍在排队/处理中的任务。
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.storage_retention_days)
    old_tasks = (
        await session.execute(
            select(models.GenerationTask)
            .where(
                models.GenerationTask.created_at < cutoff,
                models.GenerationTask.status.in_(["completed", "failed"]),
            )
            .order_by(models.GenerationTask.created_at)
        )
    ).scalars().all()
    for task in old_tasks:
        await _purge_task(session, task)
    return len(old_tasks)


async def _enforce_capacity(session, max_bytes: int) -> int:
    """While storage exceeds ``max_bytes``, delete oldest terminal tasks. Returns count."""
    removed = 0
    while get_storage_size_bytes() > max_bytes:
        oldest = (
            await session.execute(
                select(models.GenerationTask)
                .where(models.GenerationTask.status.in_(["completed", "failed"]))
                .order_by(models.GenerationTask.created_at)
                .limit(1)
            )
        ).scalars().first()
        if oldest is None:
            break  # no more terminal tasks left to delete, give up
        await _purge_task(session, oldest)
        removed += 1
    return removed


def _write_alert_state(size_bytes: int, expired: int, capacity_removed: int) -> dict:
    max_bytes = settings.storage_max_mb * 1024 * 1024
    warn_bytes = int(max_bytes * settings.storage_warn_ratio)
    state = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "storage_bytes": size_bytes,
        "storage_mb": round(size_bytes / 1024 / 1024, 2),
        "max_mb": settings.storage_max_mb,
        "warn_mb": round(warn_bytes / 1024 / 1024, 2),
        "ratio": round(size_bytes / max_bytes, 4) if max_bytes else 0.0,
        "over_warn": size_bytes >= warn_bytes,
        "over_max": size_bytes >= max_bytes,
        "expired_tasks_deleted": expired,
        "capacity_tasks_deleted": capacity_removed,
    }
    try:
        os.makedirs(settings.data_dir, exist_ok=True)
        with open(_ALERT_PATH, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
    except OSError:
        logger.warning("Failed to write storage alert state to %s", _ALERT_PATH)
    return state


async def run_maintenance_once() -> dict:
    """Run a full maintenance pass. Safe to call repeatedly."""
    expired = 0
    capacity_removed = 0
    try:
        async with async_session_maker() as session:
            expired = await _cleanup_expired(session)
            await session.commit()
            capacity_removed = await _enforce_capacity(
                session, settings.storage_max_mb * 1024 * 1024
            )
            await session.commit()
    except Exception:
        logger.exception("Maintenance pass failed")

    size = get_storage_size_bytes()
    state = _write_alert_state(size, expired, capacity_removed)
    logger.info(
        "Storage maintenance done: %.2fMB/%.0fMB (%.1f%%) | expired=%d capacity=%d",
        state["storage_mb"], state["max_mb"], state["ratio"] * 100,
        expired, capacity_removed,
    )
    return state


async def run_maintenance_loop() -> None:
    """Background loop: one pass immediately, then every N hours."""
    interval = max(1, settings.storage_cleanup_interval_hours) * 3600
    while True:
        try:
            await run_maintenance_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Maintenance loop iteration failed")
        await asyncio.sleep(interval)


def storage_has_room() -> tuple[bool, float]:
    """Quick capacity check for request-time gatekeeping.

    Returns ``(has_room, ratio)`` where ``has_room`` is False once the storage
    directory already exceeds ``storage_max_mb`` (caller should reject uploads).
    """
    size = get_storage_size_bytes()
    max_bytes = settings.storage_max_mb * 1024 * 1024
    ratio = (size / max_bytes) if max_bytes else 0.0
    return size < max_bytes, ratio
