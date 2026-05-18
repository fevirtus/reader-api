from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any

from fastapi import Depends, HTTPException, Request
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from jose import JWTError, jwt
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db_session

logger = logging.getLogger(__name__)

SESSION_COOKIE_KEYS = [
    "next-auth.session-token",
    "__Secure-next-auth.session-token",
    "authjs.session-token",
    "__Secure-authjs.session-token",
    "reader_access_token",
]

ACCESS_TOKEN_TTL_SECONDS = 7 * 24 * 60 * 60
GOOGLE_TOKEN_CLOCK_SKEW_SECONDS = 60


def _google_token_audiences_to_try(token: str) -> list[str | None]:
    audiences: list[str | None] = []
    seen: set[str] = set()

    def add(value: str | None) -> None:
        if value is None:
            if None not in audiences:
                audiences.append(None)
            return
        cleaned = value.strip()
        if not cleaned or cleaned in seen:
            return
        seen.add(cleaned)
        audiences.append(cleaned)

    for client_id in settings.google_client_id_list:
        add(client_id)

    try:
        claims = jwt.get_unverified_claims(token)
        for key in ("aud", "azp"):
            raw = claims.get(key)
            if isinstance(raw, str):
                add(raw)
            elif isinstance(raw, list):
                for item in raw:
                    if isinstance(item, str):
                        add(item)
    except Exception:
        pass

    if not audiences:
        audiences.append(None)
    return audiences


def verify_google_id_token(raw_token: str) -> dict[str, Any]:
    token = raw_token.strip()
    if token.count(".") != 2:
        raise HTTPException(status_code=400, detail="googleIdToken must be a JWT")

    request = google_requests.Request()
    last_exc: Exception | None = None

    for audience in _google_token_audiences_to_try(token):
        try:
            id_info = google_id_token.verify_oauth2_token(
                token,
                request,
                audience,
                clock_skew_in_seconds=GOOGLE_TOKEN_CLOCK_SKEW_SECONDS,
            )
            aud = id_info.get("aud")
            allowed = set(settings.google_client_id_list)
            if allowed:
                aud_values: set[str] = set()
                if isinstance(aud, str):
                    aud_values.add(aud)
                elif isinstance(aud, list):
                    aud_values.update(str(item) for item in aud)
                azp = id_info.get("azp")
                if isinstance(azp, str):
                    aud_values.add(azp)
                if aud_values.isdisjoint(allowed):
                    last_exc = ValueError(f"token audience not allowed: {aud_values}")
                    continue
            return id_info
        except Exception as exc:
            last_exc = exc
            continue

    try:
        claims = jwt.get_unverified_claims(token)
        logger.warning(
            "google id token rejected len=%s iss=%s aud=%s azp=%s exp=%s err=%s",
            len(token),
            claims.get("iss"),
            claims.get("aud"),
            claims.get("azp"),
            claims.get("exp"),
            last_exc,
        )
    except Exception:
        logger.warning("google id token rejected len=%s err=%s", len(token), last_exc)

    err_text = str(last_exc or "").lower()
    if any(x in err_text for x in ("certificate", "connection", "timeout", "urlopen", "ssl", "network")):
        raise HTTPException(
            status_code=503,
            detail="Unable to verify Google token (reader-api cannot reach googleapis.com)",
        ) from last_exc

    raise HTTPException(status_code=401, detail="Invalid Google token") from last_exc


def _jwt_secret() -> str:
    return settings.mobile_jwt_secret or settings.nextauth_secret


def create_access_token(user_id: str) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    payload = {
        "sub": user_id,
        "iat": int(now.timestamp()),
        "exp": int((now + dt.timedelta(seconds=ACCESS_TOKEN_TTL_SECONDS)).timestamp()),
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
    if row:
        return dict(row)

    # Support NextAuth/Auth.js JWT session cookies when frontend runs in JWT mode.
    secret = _jwt_secret()
    if not secret:
        return None

    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
    except JWTError:
        return None

    subject = payload.get("sub") or payload.get("id")
    if not isinstance(subject, str) or not subject:
        return None

    role = payload.get("role")
    if isinstance(role, str) and role:
        return {
            "id": subject,
            "email": payload.get("email"),
            "name": payload.get("name"),
            "image": payload.get("picture") or payload.get("image"),
            "role": role,
        }

    return await _get_user_by_id(db, subject)


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


async def require_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
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
