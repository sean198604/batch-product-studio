"""Agnes key pool + per-user daily quota helpers.

Rules implemented here (see also worker / routes):
  * Every Agnes generation is charged to ONE pool key. Resolution order:
      1. the task owner's own validated key (owner bound it in "我的 API Key")
      2. otherwise the least-recently-used *system* key (admin provided)
      3. otherwise the least-recently-used valid key from the shared pool
    -> staff who bind a valid key get unlimited use (charged to their key);
       staff without a key are limited to ``free_daily_limit`` images/day,
       charged to the system / shared pool budget.
  * Keys are per-account rate-limited in agnes.py (one token bucket per key).
  * Daily usage is DERIVED (count of TaskItems with status=success created on
    the Beijing-calendar day), so deleting an image frees its quota slot and no
    separate counter table can drift out of sync.

System key seeding
------------------
data/settings.json holds the admin's ``agnes_api_key`` plus the optional
``agnes_extra_keys`` array (extra system keys). On startup (and after any admin
settings save) we upsert those into the pool as ``source='system'`` rows. Keys
added from the admin "Key 池" card are stored with ``note='后台添加'`` and are
never auto-removed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import settings_store
from app.config import settings
from app.db import async_session_maker
from app.models import ApiKey, TaskItem, User

logger = logging.getLogger("pool")

# 业务日历用中国时区（UTC+8），与容器所在时区解耦。
CN_TZ = timezone(timedelta(hours=8))

_SYSTEM_CFG_NOTE = "系统 Key（配置下发）"
_SYSTEM_MANUAL_NOTE = "后台添加"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def today_start_utc() -> datetime:
    """Beijing-midnight expressed as a *naive* UTC datetime.

    SQLAlchemy's SQLite dialect stores our ``created_at`` (aware UTC from
    _utcnow) as a naive UTC string, and reading it back yields a naive
    datetime. So the quota boundary must also be naive UTC or Python would
    raise TypeError comparing naive vs aware values.
    """
    now_utc = datetime.now(timezone.utc)
    now_cn = now_utc.astimezone(CN_TZ)
    start_cn = now_cn.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_cn.astimezone(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------- #
# System-key seeding
# --------------------------------------------------------------------------- #
async def _seed_system_keys(session: AsyncSession) -> None:
    """Upsert keys from settings.json into the pool (idempotent)."""
    wanted: list[str] = []
    k1 = settings_store.get_agnes_key().strip()
    if k1:
        wanted.append(k1)
    for k in settings_store.get_agnes_extra_keys():
        if k:
            wanted.append(k)
    if not wanted:
        return
    existing = (
        await session.execute(
            select(ApiKey).where(
                ApiKey.provider == "agnes",
                ApiKey.source == "system",
                ApiKey.key_value.in_(wanted),
            )
        )
    ).scalars().all()
    have = {row.key_value for row in existing}
    for raw in wanted:
        if raw in have:
            continue
        session.add(
            ApiKey(
                provider="agnes",
                source="system",
                key_value=raw,
                status="valid",  # admin 提供即视为可用；可在后台逐条测试
                note=_SYSTEM_CFG_NOTE,
                validated_at=_utcnow(),
            )
        )
        logger.info("Seeded system Agnes key into pool: %s", settings_store.mask_key(raw))
    await session.commit()


async def sync_system_keys_from_settings() -> None:
    """Idempotent startup / post-save sync of configured system keys."""
    async with async_session_maker() as session:
        await _seed_system_keys(session)
        # Clean up rows that were seeded from config but whose config entry was
        # later replaced/removed. Manually-added rows ("后台添加") are kept.
        cfg_keys: set[str] = set()
        k1 = settings_store.get_agnes_key().strip()
        if k1:
            cfg_keys.add(k1)
        cfg_keys.update(settings_store.get_agnes_extra_keys())
        stale = (
            await session.execute(
                select(ApiKey).where(
                    ApiKey.provider == "agnes",
                    ApiKey.source == "system",
                    ApiKey.note == _SYSTEM_CFG_NOTE,
                )
            )
        ).scalars().all()
        for row in stale:
            if row.key_value not in cfg_keys:
                await session.delete(row)
                logger.info("Removed stale system Agnes key %s from pool", row.id)
        await session.commit()


# --------------------------------------------------------------------------- #
# Key resolution for generation
# --------------------------------------------------------------------------- #
async def own_valid_key(
    session: AsyncSession, user_id: int
) -> "ApiKey | None":
    """The caller's own validated pool key, if any."""
    row = (
        await session.execute(
            select(ApiKey)
            .where(
                ApiKey.provider == "agnes",
                ApiKey.owner_user_id == user_id,
                ApiKey.status == "valid",
            )
            .order_by(ApiKey.id.desc())
            .limit(1)
        )
    ).scalars().first()
    return row


async def pick_pool_key(
    session: AsyncSession, user_id: int
) -> "ApiKey | None":
    """Choose the key to charge one generation to (own -> system -> pool LRU)."""
    own = await own_valid_key(session, user_id)
    if own is not None:
        return own
    rows = (
        await session.execute(
            select(ApiKey)
            .where(
                ApiKey.provider == "agnes",
                ApiKey.status == "valid",
            )
            .order_by(
                # 系统 Key 优先；同组内最久未用者优先（近似轮换）。
                (ApiKey.source == "system").desc(),
                ApiKey.last_used_at.asc().nullsfirst(),
                ApiKey.id.asc(),
            )
        )
    ).scalars().all()
    return rows[0] if rows else None


async def mark_key_used(session: AsyncSession, key: "ApiKey | None") -> None:
    if key is not None:
        key.last_used_at = _utcnow()


async def mark_key_invalid(
    session: AsyncSession, key: "ApiKey | None", note: str
) -> None:
    if key is not None:
        key.status = "invalid"
        key.note = (note or "")[:300]
        logger.warning("Pool key #%s marked invalid: %s", key.id, note)


# --------------------------------------------------------------------------- #
# Daily quota
# --------------------------------------------------------------------------- #
async def has_unlimited(session: AsyncSession, user: User) -> bool:
    """Admins and staff with a bound valid key are not subject to the cap."""
    if user.role == "admin":
        return True
    return await own_valid_key(session, user.id) is not None


async def used_today(session: AsyncSession, user_id: int) -> int:
    """Number of images successfully generated by this user today (Beijing)."""
    n = (
        await session.execute(
            select(TaskItem.id)
            .where(
                TaskItem.user_id == user_id,
                TaskItem.status == "success",
                TaskItem.created_at >= today_start_utc(),
            )
        )
    ).scalars().all()
    return len(n)


async def quota_state(session: AsyncSession, user: User) -> dict:
    """{unlimited, used_today, limit} for the given user."""
    limit = settings_store.get_free_daily_limit()
    if await has_unlimited(session, user):
        return {"unlimited": True, "used_today": 0, "limit": limit}
    return {
        "unlimited": False,
        "used_today": await used_today(session, user.id),
        "limit": limit,
    }


async def pool_summary() -> dict:
    """Public, non-secret pool metrics (valid/system/user key counts)."""
    async with async_session_maker() as session:
        rows = (
            await session.execute(
                select(ApiKey).where(
                    ApiKey.provider == "agnes", ApiKey.status == "valid"
                )
            )
        ).scalars().all()
    return {
        "valid": len(rows),
        "system": sum(1 for r in rows if r.source == "system"),
        "user": sum(1 for r in rows if r.source == "user"),
    }
