from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass
from typing import Optional

import requests
from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .database.models import CourseMembership

_TOKEN_CACHE: dict[str, tuple[float, str]] = {}
_TOKEN_CACHE_TTL_SECONDS = int(os.getenv("AUTH_TOKEN_CACHE_TTL_SECONDS", "60"))


@dataclass(frozen=True)
class AuthContext:
    user_id: str
    access_token: str


def _cfg() -> tuple[str, str]:
    url = (os.getenv("SUPABASE_URL") or "").rstrip("/")
    key = (os.getenv("SUPABASE_API_KEY") or os.getenv("SUPABASE_ANON_KEY") or "").strip()
    if not url:
        raise RuntimeError("SUPABASE_URL is required.")
    if not key:
        raise RuntimeError("SUPABASE_API_KEY (or SUPABASE_ANON_KEY) is required.")
    return url, key


def validate_required_env() -> None:
    _cfg()
    required = (
        "SUPABASE_DB_URL",
        "OPENAI_API_KEY",
    )
    missing = [name for name in required if not (os.getenv(name) or "").strip()]
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(f"Missing required environment variables: {joined}")


def _auth_header(request: Request) -> str:
    value = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    if not value.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = value.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return token


def _cache_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _cached_user_id(token: str) -> Optional[str]:
    key = _cache_key(token)
    row = _TOKEN_CACHE.get(key)
    if not row:
        return None
    expires_at, user_id = row
    if time.time() >= expires_at:
        _TOKEN_CACHE.pop(key, None)
        return None
    return user_id


def _set_cached_user_id(token: str, user_id: str) -> None:
    key = _cache_key(token)
    _TOKEN_CACHE[key] = (time.time() + _TOKEN_CACHE_TTL_SECONDS, user_id)


def resolve_auth_context(request: Request) -> AuthContext:
    token = _auth_header(request)
    user_id = _cached_user_id(token)
    if user_id:
        return AuthContext(user_id=user_id, access_token=token)

    url, key = _cfg()
    resp = requests.get(
        f"{url}/auth/v1/user",
        headers={
            "Authorization": f"Bearer {token}",
            "apikey": key,
            "Accept": "application/json",
        },
        timeout=15,
    )
    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid bearer token")
    body = resp.json() if resp.content else {}
    user_id = str(body.get("id") or "").strip()
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid bearer token")
    _set_cached_user_id(token, user_id)
    return AuthContext(user_id=user_id, access_token=token)


async def has_membership(
    db_session: AsyncSession,
    *,
    user_id: str,
    search_space_id: int,
    role: Optional[str] = None,
) -> bool:
    stmt = select(CourseMembership).where(
        CourseMembership.user_id == user_id,
        CourseMembership.search_space_id == search_space_id,
    )
    if role:
        stmt = stmt.where(CourseMembership.role == role)
    result = await db_session.execute(stmt)
    return result.scalars().first() is not None
