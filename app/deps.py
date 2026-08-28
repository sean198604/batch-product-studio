"""Shared FastAPI dependencies: DB session, current user, admin guard."""
from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.models import User
from app.security import decode_token

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

_credentials_exc = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials.",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    session: AsyncSession = Depends(get_session),
) -> User:
    """Resolve the JWT into a live User row (re-checks role from DB)."""
    try:
        payload = decode_token(token)
        user_id = int(payload.get("sub"))
    except (Exception,):
        raise _credentials_exc

    user = await session.get(User, user_id)
    if user is None:
        raise _credentials_exc
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    """Dependency that only admits admin-role users."""
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator privileges required.",
        )
    return user
