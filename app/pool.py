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
    """Upsert keys from settings.json into the pool (idempotent).

    同时处理国内站（agnes_*）与国际站（agnes_intl_*）两套系统 Key，
    各自带 station 标记后入库，互不串用。
    """
    # (station, [key, ...])：两套配置分别播种。
    wanted_by_station: "dict[str, list[str]]" = {
        "cn": [],
        "intl": [],
    }
    for k in (settings_store.get_agnes_key().strip(), *settings_store.get_agnes_extra_keys()):
        if k:
            wanted_by_station["cn"].append(k)
    for k in (settings_store.get_agnes_key("intl").strip(), *settings_store.get_agnes_extra_keys("intl")):
        if k:
            wanted_by_station["intl"].append(k)
    if not any(wanted_by_station.values()):
        return
    for station, wanted in wanted_by_station.items():
        if not wanted:
            continue
        existing = (
            await session.execute(
                select(ApiKey).where(
                    ApiKey.provider == "agnes",
                    ApiKey.source == "system",
                    ApiKey.station == station,
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
                    station=station,
                    key_value=raw,
                    status="valid",  # admin 提供即视为可用；可在后台逐条测试
                    note=_SYSTEM_CFG_NOTE,
                    validated_at=_utcnow(),
                )
            )
            logger.info(
                "Seeded system Agnes %s key into pool: %s",
                "国际站" if station == "intl" else "国内站",
                settings_store.mask_key(raw),
            )
    await session.commit()


async def sync_system_keys_from_settings() -> None:
    """Idempotent startup / post-save sync of configured system keys."""
    async with async_session_maker() as session:
        await _seed_system_keys(session)
        # Clean up rows that were seeded from config but whose config entry was
        # later replaced/removed. Manually-added rows ("后台添加") are kept.
        # 按 station 分别比对：国内站配置只清国内站的 stale 行，国际站同理。
        cfg_keys: "dict[str, set[str]]" = {"cn": set(), "intl": set()}
        for k in (settings_store.get_agnes_key().strip(), *settings_store.get_agnes_extra_keys()):
            if k:
                cfg_keys["cn"].add(k)
        for k in (settings_store.get_agnes_key("intl").strip(), *settings_store.get_agnes_extra_keys("intl")):
            if k:
                cfg_keys["intl"].add(k)
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
            station = row.station or "cn"
            if row.key_value not in cfg_keys.get(station, set()):
                await session.delete(row)
                logger.info("Removed stale system Agnes %s key %s from pool", station, row.id)
        await session.commit()


# --------------------------------------------------------------------------- #
# Key resolution for generation
# --------------------------------------------------------------------------- #
async def own_valid_key(
    session: AsyncSession, user_id: int, station: "str | None" = None
) -> "ApiKey | None":
    """The caller's own validated pool key, if any.

    *station* 限定站点（"cn"/"intl"）；为 None 时返回任意站点的首个有效 Key
    （用于「我的 API Key」展示用户已绑定的 Key 及其站点）。
    """
    conds = [
        ApiKey.provider == "agnes",
        ApiKey.owner_user_id == user_id,
        ApiKey.status == "valid",
    ]
    if station is not None:
        conds.append(ApiKey.station == station)
    row = (
        await session.execute(
            select(ApiKey)
            .where(*conds)
            .order_by(ApiKey.id.desc())
            .limit(1)
        )
    ).scalars().first()
    return row


async def pick_pool_key(
    session: AsyncSession, user_id: int, station: str = "cn"
) -> "ApiKey | None":
    """Choose the key to charge one generation to (own -> system -> pool LRU).

    仅在本 *station* 的 Key 池内解析：国内站任务用国内站 Key，国际站任务用
    国际站 Key，互不串用。
    """
    own = await own_valid_key(session, user_id, station)
    if own is not None:
        return own
    rows = (
        await session.execute(
            select(ApiKey)
            .where(
                ApiKey.provider == "agnes",
                ApiKey.station == station,
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
async def has_unlimited(session: AsyncSession, user: User, station: str = "cn") -> bool:
    """Admins and staff with a bound valid key are not subject to the cap.

    不限量判定按 *station* 隔离：绑定了国内站 Key 只解锁国内站不限量；
    国际站任务仍需该站点有绑定的有效 Key（或系统 Key）。
    """
    if user.role == "admin":
        return True
    return await own_valid_key(session, user.id, station) is not None


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


async def pool_summary(station: "str | None" = None) -> dict:
    """Public, non-secret pool metrics (valid/system/user key counts).

    *station* 限定统计某站点（"cn"/"intl"）；为 None 时统计全部 Agnes Key。
    """
    async with async_session_maker() as session:
        conds = [ApiKey.provider == "agnes", ApiKey.status == "valid"]
        if station is not None:
            conds.append(ApiKey.station == station)
        rows = (
            await session.execute(select(ApiKey).where(*conds))
        ).scalars().all()
    return {
        "valid": len(rows),
        "system": sum(1 for r in rows if r.source == "system"),
        "user": sum(1 for r in rows if r.source == "user"),
    }
