"""F2 isolated-stack GoTrue stub — the minimal /auth/v1 surface the campaign
actually touches, so the isolated f2-postgres pair needs no real Supabase
auth container (coordinator decision 2026-07-04, option 2).

Exactly three endpoints are consumed anywhere in the harness/backend:

  1. POST /auth/v1/admin/users            (bootstrap_course.mint_teacher,
                                           campaign.cast.student.mint_student_token)
  2. POST /auth/v1/token?grant_type=password  (same two callers)
  3. GET  /auth/v1/user                   (auth.resolve_auth_context — the
                                           backend's only auth call)

Semantics preserved from real GoTrue as far as the callers observe:
  - admin/users create is idempotent per email (re-create is a no-op).
  - token response carries {"access_token": <JWT with a "sub" claim>} —
    bootstrap_course.py decodes the JWT payload's "sub" for the user id.
  - /auth/v1/user returns {"id": <uuid>} for a valid bearer token, 401 else.
  - every minted user is INSERTed into auth.users so the migrations' FK
    constraints (apollo_sessions.user_id -> auth.users etc.) hold.

User ids are uuid5(NS_URL, email) — deterministic, so re-runs of the same
persona re-use the same identity (matches mint_student_token's idempotent
create-then-signin contract).

JWTs are HS256 over the local-dev demo secret via stdlib hmac (no pyjwt in
the venv); only this stub ever verifies them.

Usage:
    .venv/Scripts/python.exe campaign/out/f2/infra/auth_stub_server.py
Serves on 127.0.0.1:57421. DB: postgresql://postgres:postgres@127.0.0.1:57422.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid

import asyncpg
import uvicorn
from fastapi import FastAPI, HTTPException, Request

DB_DSN = "postgresql://postgres:postgres@127.0.0.1:57422/postgres"
JWT_SECRET = "super-secret-jwt-token-with-at-least-32-characters-long"
TOKEN_TTL_SECONDS = 24 * 3600

app = FastAPI()
_pool: asyncpg.Pool | None = None


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def encode_jwt(payload: dict) -> str:
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64url(json.dumps(payload).encode())
    signing_input = f"{header}.{body}".encode()
    sig = _b64url(hmac.new(JWT_SECRET.encode(), signing_input, hashlib.sha256).digest())
    return f"{header}.{body}.{sig}"


def decode_jwt(token: str) -> dict:
    try:
        header, body, sig = token.split(".")
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="malformed token") from exc
    signing_input = f"{header}.{body}".encode()
    expected = _b64url(hmac.new(JWT_SECRET.encode(), signing_input, hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        raise HTTPException(status_code=401, detail="bad signature")
    payload = json.loads(_b64url_decode(body))
    if payload.get("exp", 0) < time.time():
        raise HTTPException(status_code=401, detail="token expired")
    return payload


def user_id_for(email: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"f2-campaign:{email.lower()}"))


async def _ensure_user(email: str) -> str:
    uid = user_id_for(email)
    assert _pool is not None
    async with _pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO auth.users (id, email) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING",
            uuid.UUID(uid),
            email.lower(),
        )
    return uid


@app.on_event("startup")
async def _startup() -> None:
    global _pool
    _pool = await asyncpg.create_pool(DB_DSN, min_size=1, max_size=4)


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _pool is not None:
        await _pool.close()


@app.post("/auth/v1/admin/users")
async def admin_create_user(request: Request):
    body = await request.json()
    email = (body.get("email") or "").strip()
    if not email:
        raise HTTPException(status_code=400, detail="email required")
    uid = await _ensure_user(email)
    return {"id": uid, "email": email.lower()}


@app.post("/auth/v1/token")
async def token(request: Request):
    if request.query_params.get("grant_type") != "password":
        raise HTTPException(status_code=400, detail="unsupported grant_type")
    body = await request.json()
    email = (body.get("email") or "").strip()
    if not email or not body.get("password"):
        raise HTTPException(status_code=400, detail="email and password required")
    uid = await _ensure_user(email)
    now = int(time.time())
    access_token = encode_jwt(
        {
            "sub": uid,
            "email": email.lower(),
            "role": "authenticated",
            "aud": "authenticated",
            "iat": now,
            "exp": now + TOKEN_TTL_SECONDS,
        }
    )
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": TOKEN_TTL_SECONDS,
        "user": {"id": uid, "email": email.lower()},
    }


@app.get("/auth/v1/user")
async def get_user(request: Request):
    value = request.headers.get("authorization") or ""
    if not value.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer")
    payload = decode_jwt(value.split(" ", 1)[1].strip())
    return {"id": payload["sub"], "email": payload.get("email", "")}


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=57421, log_level="warning")
