from __future__ import annotations

import datetime as dt
import os
from typing import Any

from fastapi import Depends, HTTPException, Request
from jose import JWTError, jwt
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db_session

SESSION_COOKIE_KEYS = [
    "next-auth.session-token",
    "__Secure-next-auth.session-token",
    "authjs.session-token",
    "__Secure-authjs.session-token",
]


def _jwt_secret() -> str:
    return settings.mobile_jwt_secret or settings.nextauth_secret


def create_access_token(user_id: str) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    payload = {
        "sub": user_id,
        "iat": int(now.timestamp()),
        "exp": int((now + dt.timedelta(days=7)).timestamp()),
    }
    secret = _jwt_secret()
    if not secret:
        raise RuntimeError("Missing MOBILE_JWT_SECRET or NEXTAUTH_SECRET")
    return jwt.encode(payload, secret, algorithm="HS256")


async def _get_user_by_id(db: AsyncSession, user_id: str) -> dict[str, Any] | None:
    result = await db.execute(
        text(
            'SELECT id, email, name, image, role FROM "User" WHERE id = :user_id LIMIT 1'
        ),
        {"user_id": user_id},
    )
    row = result.mappings().first()
    return dict(row) if row else None


async def _get_user_from_session_cookie(db: AsyncSession, request: Request) -> dict[str, Any] | None:
    token = None
    for key in SESSION_COOKIE_KEYS:
        value = request.cookies.get(key)
        if value:
            token = value
            break

    if not token:
        return None

    result = await db.execute(
        text(
            'SELECT u.id, u.email, u.name, u.image, u.role '
            'FROM "Session" s '
            'JOIN "User" u ON u.id = s."userId" '
            'WHERE s."sessionToken" = :token AND s.expires > NOW() '
            'LIMIT 1'
        ),
        {"token": token},
    )
    row = result.mappings().first()
    return dict(row) if row else None


async def resolve_current_user(db: AsyncSession, request: Request) -> dict[str, Any] | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1].strip()
        secret = _jwt_secret()
        if not secret:
            return None
        try:
            payload = jwt.decode(token, secret, algorithms=["HS256"])
            subject = payload.get("sub")
            if isinstance(subject, str) and subject:
                return await _get_user_by_id(db, subject)
        except JWTError:
            return None

    return await _get_user_from_session_cookie(db, request)


async def require_current_user(db: AsyncSession, request: Request) -> dict[str, Any]:
    user = await resolve_current_user(db, request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return user


async def require_mod_user(request: Request, db: AsyncSession = Depends(get_db_session)) -> dict[str, Any]:
    user = await resolve_current_user(db, request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if user.get("role") not in ("MOD", "ADMIN"):
        raise HTTPException(status_code=403, detail="Forbidden: MOD or ADMIN role required")
    return user
