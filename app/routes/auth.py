"""Authentication routes: register, login, current-user info, registration mode."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.config import settings
from app.db import get_session
from app.deps import get_current_user
from app.models import (
    RegisterRequest,
    RegistrationCode,
    RegistrationMode,
    TokenResponse,
    User,
    UserRead,
)
from app.security import (
    create_access_token,
    hash_password,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _is_code_acceptable(code_row: RegistrationCode) -> bool:
    """True iff the code is active, unexpired, and has uses remaining."""
    if code_row.status != "active":
        return False
    if code_row.used_count >= code_row.max_uses:
        return False
    if code_row.expires_at is not None and code_row.expires_at <= _utcnow():
        return False
    return True


@router.get("/registration-mode", response_model=RegistrationMode)
async def registration_mode(
    session: AsyncSession = Depends(get_session),
) -> RegistrationMode:
    """Tell the frontend whether the invite-code field is currently required.

    The threshold is read from runtime config (``registration_open_user_threshold``),
    so adjusting it + restart makes the gate tighten or relax without code changes.
    """
    threshold = max(0, int(settings.registration_open_user_threshold))
    current_count = (
        await session.execute(select(func.count(models.User.id)))
    ).scalar() or 0
    return RegistrationMode(
        code_required=current_count >= threshold,
        current_count=int(current_count),
        threshold=threshold,
    )


@router.post("/register", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterRequest,
    session: AsyncSession = Depends(get_session),
) -> UserRead:
    """Register a new employee.

    * First registrant becomes admin.
    * Once user count reaches ``registration_open_user_threshold``, callers must
      provide a valid registration code. We count it as 1 use atomically in the
      same commit as the new user insert (no off-by-one even under concurrency).
    """
    existing = await session.execute(
        select(User).where(User.username == body.username)
    )
    if existing.scalars().first() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username already exists.",
        )

    # 禁止空用户名 / 纯空白用户名：历史遗留的 '' 用户就是这样混进来的。
    # 统一 trim 后落库，避免「用户名没名字」的空壳账号再次出现。
    username = (body.username or "").strip()
    if not username:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="用户名不能为空。",
        )
    if len(username) > 32:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="用户名最长 32 个字符。",
        )
    if username != body.username:
        # trim 后可能撞上已存在的账号（如 "alice" vs "alice "）。
        dup = (
            await session.execute(select(User).where(User.username == username))
        ).scalars().first()
        if dup is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Username already exists.",
            )

    current_count = (
        await session.execute(select(func.count(User.id)))
    ).scalar() or 0

    threshold = max(0, int(settings.registration_open_user_threshold))
    needs_code = current_count >= threshold

    code_row: Optional[RegistrationCode] = None
    if needs_code:
        raw = (body.registration_code or "").strip()
        if not raw:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "当前注册用户已达上限，请向管理员索要注册邀请码后再注册。"
                ),
            )
        # Case-insensitive lookup so codes like "AB12CD34EF56" still work when
        # the admin issued them as "ab12cd34ef56".
        candidate = (
            await session.execute(
                select(RegistrationCode).where(
                    RegistrationCode.code == raw.upper()
                )
            )
        ).scalars().first()
        if candidate is None or not _is_code_acceptable(candidate):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="注册邀请码无效、已过期或次数已用完，请联系管理员。",
            )
        code_row = candidate

    # First registrant becomes admin; everyone else is staff.
    role = "admin" if current_count == 0 else "staff"

    user = User(
        username=username,  # 已 trim，杜绝空白用户名
        password_hash=hash_password(body.password),
        role=role,
    )
    session.add(user)
    await session.flush()  # populate user.id without committing yet

    if code_row is not None:
        code_row.used_count = (code_row.used_count or 0) + 1
        code_row.last_used_at = _utcnow()
        if code_row.used_count >= code_row.max_uses:
            # Auto-disable when fully consumed to keep the pool tidy.
            code_row.status = "disabled"

    await session.commit()
    await session.refresh(user)
    return UserRead.model_validate(user)


@router.post("/login", response_model=TokenResponse)
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    """Exchange username+password for a JWT bearer token."""
    result = await session.execute(
        select(User).where(User.username == form_data.username)
    )
    user = result.scalars().first()
    if user is None or not verify_password(form_data.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = create_access_token(user.id, user.role)
    return TokenResponse(access_token=token)


@router.get("/me", response_model=UserRead)
async def me(user: User = Depends(get_current_user)) -> UserRead:
    """Return the currently authenticated user's profile."""
    return UserRead.model_validate(user)
