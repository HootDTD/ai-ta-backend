"""FastAPI auth dependencies for the /apollo router (Phase-1 retrofit).

Closes the security.md "Known gaps" item: /apollo/* previously took identity
from the request body with no token validation. Every endpoint now requires a
Supabase bearer token; session-scoped endpoints verify the caller owns the
session; session creation verifies course membership.

Deliberately NOT reusing server.py's _require_course_membership — that helper
is sync and drives its own event loop via run_async, which deadlocks inside
async endpoints. These dependencies are natively async and reuse the same
auth.py primitives.
"""
from __future__ import annotations

import asyncio

import requests
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.persistence.models import ApolloSession
from auth import (
    AuthContext,
    auto_enroll_student_membership,
    has_membership,
    resolve_auth_context,
)
from database.session import get_db_session


async def require_user(request: Request) -> AuthContext:
    """Resolve the bearer token to an AuthContext (401 on failure).

    resolve_auth_context uses the blocking `requests` client (with its own
    TTL cache), so it runs off the event loop.
    """
    try:
        return await asyncio.to_thread(resolve_auth_context, request)
    except HTTPException:
        raise
    except RuntimeError as exc:  # missing SUPABASE_* config
        raise HTTPException(
            status_code=500, detail="Server auth configuration error"
        ) from exc
    except requests.exceptions.RequestException as exc:  # GoTrue unreachable
        raise HTTPException(
            status_code=503, detail="Auth service unavailable"
        ) from exc


async def require_course_member(
    *,
    db: AsyncSession,
    auth: AuthContext,
    search_space_id: int,
) -> None:
    """403 unless the user belongs to the course.

    Mirrors server.py semantics: authenticated users may auto-enroll as
    students where the env allows it (AUTO_ENROLL_STUDENT_MEMBERSHIP).
    """
    if await has_membership(db, user_id=auth.user_id, search_space_id=search_space_id):
        return
    enrolled = await auto_enroll_student_membership(
        db, user_id=auth.user_id, search_space_id=search_space_id
    )
    # Defensive re-check: the return value of auto_enroll is not blindly
    # trusted; membership is re-verified from the DB to guard against future
    # drift in auto_enroll's contract.
    if enrolled and await has_membership(
        db, user_id=auth.user_id, search_space_id=search_space_id
    ):
        return
    raise HTTPException(status_code=403, detail="Forbidden for this course")


async def require_session_owner(
    session_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> AuthContext:
    """401/403/404 gate for /apollo/sessions/{session_id}/* endpoints.

    FastAPI resolves session_id from the path. Returns the AuthContext so
    handlers can use the validated identity.
    """
    auth = await require_user(request)
    row = (
        (await db.execute(select(ApolloSession).where(ApolloSession.id == session_id)))
        .scalars()
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if str(row.user_id) != str(auth.user_id):
        raise HTTPException(status_code=403, detail="Not your session")
    return auth
