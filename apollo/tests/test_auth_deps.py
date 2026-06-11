"""Unit tests for the Apollo auth dependencies (Phase-1 retrofit).

resolve_auth_context is monkeypatched — these tests cover the ownership and
membership logic, not GoTrue token validation (auth.py owns that).
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import apollo.auth_deps as deps
from apollo.conftest import TEST_SPACE_ID, TEST_USER_ID, TEST_USER_ID_2
from apollo.persistence.models import ApolloSession
from auth import AuthContext
from database.models import Base


def _fake_request() -> Request:
    return Request(scope={"type": "http", "headers": []})


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sc: Base.metadata.create_all(sc, tables=[ApolloSession.__table__])
        )
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        yield s
    await engine.dispose()


@pytest.fixture
def as_user(monkeypatch):
    def _set(user_id: str):
        monkeypatch.setattr(
            deps, "resolve_auth_context",
            lambda _request: AuthContext(user_id=user_id, access_token="tok"),
        )
    return _set


async def _make_session(db: AsyncSession, user_id: str) -> int:
    row = ApolloSession(
        user_id=user_id, search_space_id=TEST_SPACE_ID,
        status="active", phase="TEACHING",
    )
    db.add(row)
    await db.commit()
    return row.id


@pytest.mark.asyncio
async def test_owner_passes(db, as_user):
    as_user(TEST_USER_ID)
    sid = await _make_session(db, TEST_USER_ID)
    auth = await deps.require_session_owner(sid, _fake_request(), db)
    assert auth.user_id == TEST_USER_ID


@pytest.mark.asyncio
async def test_non_owner_gets_403(db, as_user):
    as_user(TEST_USER_ID_2)
    sid = await _make_session(db, TEST_USER_ID)
    with pytest.raises(HTTPException) as exc:
        await deps.require_session_owner(sid, _fake_request(), db)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_missing_session_gets_404(db, as_user):
    as_user(TEST_USER_ID)
    with pytest.raises(HTTPException) as exc:
        await deps.require_session_owner(999999, _fake_request(), db)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_member_check_403_without_membership(db, as_user, monkeypatch):
    async def _no(*args, **kwargs):
        return False
    monkeypatch.setattr(deps, "has_membership", _no)
    monkeypatch.setattr(deps, "auto_enroll_student_membership", _no)
    with pytest.raises(HTTPException) as exc:
        await deps.require_course_member(
            db=db,
            auth=AuthContext(user_id=TEST_USER_ID, access_token="tok"),
            search_space_id=TEST_SPACE_ID,
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_member_check_passes_with_membership(db, monkeypatch):
    async def _yes(*args, **kwargs):
        return True
    monkeypatch.setattr(deps, "has_membership", _yes)
    await deps.require_course_member(
        db=db,
        auth=AuthContext(user_id=TEST_USER_ID, access_token="tok"),
        search_space_id=TEST_SPACE_ID,
    )  # no raise
