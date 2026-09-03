"""Per-user profile & Agnes API-key self-service.

Every logged-in user can bind (验证并保存) their own Agnes key. The key is
live-probed first and only enters the shared pool when the probe succeeds;
the caller then enjoys unlimited daily generation while their own key is
charged. A user without a bound key keeps a small daily free quota.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import pool, settings_store
from app.agnes import probe_agnes_key
from app.db import get_session
from app.deps import get_current_user
from app.models import ApiKey, MyProfile, ProfileKeyInfo, ProfileKeyUpdate, User
from app.pool import _utcnow

router = APIRouter(prefix="/api/profile", tags=["profile"])


def _profile_key_info(key: "ApiKey | None") -> "ProfileKeyInfo | None":
    if key is None:
        return None
    return ProfileKeyInfo(
        saved=True,
        masked=settings_store.mask_key(key.key_value),
        status=key.status,
        source=key.source,
        note=key.note,
        validated_at=key.validated_at,
    )


async def _profile(session: AsyncSession, user: User) -> MyProfile:
    own = await pool.own_valid_key(session, user.id)
    quota = await pool.quota_state(session, user)
    return MyProfile(
        id=user.id,
        username=user.username,
        role=user.role,
        agnes_key=_profile_key_info(own),
        quota_unlimited=bool(quota["unlimited"]),
        quota_used_today=int(quota["used_today"]),
        quota_limit=int(quota["limit"]),
    )


@router.get("", response_model=MyProfile)
async def get_profile(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> MyProfile:
    """Current user's key-binding status + today's quota."""
    return await _profile(session, user)


@router.put("/agnes-key", response_model=MyProfile)
async def bind_agnes_key(
    body: ProfileKeyUpdate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> MyProfile:
    """Validate and bind the caller's own Agnes key (live probe required).

    * probe OK          -> replace the user's previous key; enters the pool
    * server rejects it -> 400, nothing saved
    * 429 / network     -> 400 "暂无法验证" (NOT treated as invalid), nothing saved
    * duplicate         -> 409 (already bound to the user / another owner / system)
    """
    key = (body.api_key or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="请粘贴 Agnes API Key。")
    if len(key) < 8:
        raise HTTPException(status_code=400, detail="Key 格式不正确（长度过短）。")

    probe = await probe_agnes_key(api_key=key)

    dup = (
        await session.execute(
            select(ApiKey).where(
                ApiKey.provider == "agnes", ApiKey.key_value == key
            )
        )
    ).scalars().first()

    if not probe.get("ok"):
        if probe.get("auth_failed"):
            raise HTTPException(
                status_code=400,
                detail=f"Key 无效（服务端拒绝）：{probe.get('message','')}",
            )
        # 429 / 5xx / 网络错误：不能证明 Key 无效，提示稍后重试，不保存。
        raise HTTPException(
            status_code=400,
            detail=(
                "暂时无法验证（可能是限流或网络问题，不代表 Key 无效），"
                "请稍后重试。详情：" + str(probe.get("message", ""))
            ),
        )

    # 验证通过：
    if dup is not None:
        if dup.owner_user_id == user.id:
            dup.status = "valid"
            dup.note = "员工自绑"
            dup.validated_at = _utcnow()
            await session.commit()
        elif dup.source == "system":
            raise HTTPException(
                status_code=409,
                detail="该 Key 已是系统 Key（管理员配置），无需重复绑定。",
            )
        else:
            owner = await session.get(User, dup.owner_user_id)
            raise HTTPException(
                status_code=409,
                detail="该 Key 已被其他用户绑定（"
                + (owner.username if owner else "未知用户")
                + "），如为您本人请使用对方账号，或联系管理员。",
            )
    else:
        # 替换旧 Key：同一用户只保留最新一条有效记录。
        old = (
            await session.execute(
                select(ApiKey).where(
                    ApiKey.provider == "agnes",
                    ApiKey.owner_user_id == user.id,
                )
            )
        ).scalars().all()
        for row in old:
            await session.delete(row)
        session.add(
            ApiKey(
                provider="agnes",
                source="user",
                owner_user_id=user.id,
                key_value=key,
                status="valid",
                note="员工自绑",
                validated_at=_utcnow(),
            )
        )
        await session.commit()

    return await _profile(session, user)


@router.delete("/agnes-key", response_model=MyProfile)
async def unbind_agnes_key(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> MyProfile:
    """Remove the caller's own bound key (back to the daily free quota)."""
    rows = (
        await session.execute(
            select(ApiKey).where(
                ApiKey.provider == "agnes",
                ApiKey.owner_user_id == user.id,
                ApiKey.source == "user",
            )
        )
    ).scalars().all()
    for row in rows:
        await session.delete(row)
    await session.commit()
    return await _profile(session, user)
