"""Router-level auth wiring: no token -> 401; owner token -> handler runs.

Uses TestClient against a minimal app with the real router. The DB,
resolve_auth_context, and get_neo4j_client are overridden so the test
does not need live Supabase or Neo4j credentials.

Adaptation notes vs. the task scaffold:
- `register_exception_handlers` exists at apollo.api (confirmed).
- Session-scoped endpoints (e.g. GET /apollo/sessions/{session_id}) depend on
  BOTH `require_session_owner` AND `get_neo4j_client`. FastAPI resolves all
  Depends before the endpoint body; `get_neo4j_client` raises KeyError when
  NEO4J_* env vars are absent. We therefore override `get_neo4j_client` in
  the test app with a stub that returns None — the 403/401 gate fires during
  dependency resolution itself (before the handler body runs), so neo4j is
  never actually used by these auth tests.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import apollo.api as apollo_api
import apollo.auth_deps as deps
from apollo.api import get_neo4j_client, register_exception_handlers
from apollo.api import router as apollo_router
from apollo.conftest import TEST_SPACE_ID, TEST_USER_ID
from apollo.persistence.models import ApolloSession, StudentProgress
from database.models import Base
from database.session import get_db_session

# ---------------------------------------------------------------------------
# Shared fixture: in-memory SQLite app + DB session override
# ---------------------------------------------------------------------------


@pytest.fixture
def client_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        execution_options={"schema_translate_map": {"app": None, "internal": None}},
    )

    async def _bootstrap():
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda sc: Base.metadata.create_all(
                    sc,
                    tables=[ApolloSession.__table__, StudentProgress.__table__],
                )
            )

    asyncio.run(_bootstrap())

    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async def _override_db():
        async with Session() as s:
            yield s

    def _fake_neo():
        """Return None — auth gates fire before any neo4j call."""
        return None

    app = FastAPI()
    app.include_router(apollo_router)
    register_exception_handlers(app)
    app.dependency_overrides[get_db_session] = _override_db
    app.dependency_overrides[get_neo4j_client] = _fake_neo
    return app, Session


# ---------------------------------------------------------------------------
# Test 1: GET /apollo/progress — no token → 401
# ---------------------------------------------------------------------------


def test_progress_requires_token(client_factory):
    app, _ = client_factory
    r = TestClient(app).get("/apollo/progress")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Test 2: GET /apollo/progress — valid token → 200 with user_id in response
# ---------------------------------------------------------------------------


def test_progress_with_token_returns_defaults(client_factory, monkeypatch):
    from auth import AuthContext

    app, _ = client_factory
    monkeypatch.setattr(
        deps,
        "resolve_auth_context",
        lambda _request: AuthContext(user_id=TEST_USER_ID, access_token="tok"),
    )
    r = TestClient(app).get("/apollo/progress", headers={"Authorization": "Bearer tok"})
    assert r.status_code == 200
    assert r.json()["user_id"] == TEST_USER_ID


# ---------------------------------------------------------------------------
# Test 3: GET /apollo/sessions/{session_id} — wrong owner → 403
# ---------------------------------------------------------------------------


def test_session_endpoint_403_for_non_owner(client_factory, monkeypatch):
    from auth import AuthContext

    app, Session = client_factory

    async def _seed() -> int:
        async with Session() as s:
            row = ApolloSession(
                user_id="00000000-0000-4000-8000-0000000000ff",
                search_space_id=TEST_SPACE_ID,
                status="active",
                phase="TEACHING",
            )
            s.add(row)
            await s.commit()
            return row.id

    sid = asyncio.run(_seed())
    monkeypatch.setattr(
        deps,
        "resolve_auth_context",
        lambda _request: AuthContext(user_id=TEST_USER_ID, access_token="tok"),
    )
    r = TestClient(app).get(f"/apollo/sessions/{sid}", headers={"Authorization": "Bearer tok"})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Test 4: GET /apollo/sessions/{session_id} — no token → 401
# ---------------------------------------------------------------------------


def test_session_endpoint_requires_token(client_factory):
    app, _ = client_factory
    r = TestClient(app).get("/apollo/sessions/999")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Test 5: POST /apollo/sessions/from_hoot — valid token + membership → init runs
# ---------------------------------------------------------------------------


def test_from_hoot_happy_path_invokes_init(client_factory, monkeypatch):
    """Token resolves, course membership passes → handler calls init with the
    token-derived user_id and body search_space_id (never a body student_id)."""
    from auth import AuthContext

    app, _ = client_factory
    monkeypatch.setattr(
        deps,
        "resolve_auth_context",
        lambda _request: AuthContext(user_id=TEST_USER_ID, access_token="tok"),
    )

    async def _is_member(*args, **kwargs):
        return True

    monkeypatch.setattr(deps, "has_membership", _is_member)

    captured: dict = {}

    async def _fake_init(**kwargs):
        captured.update(kwargs)
        return {"session_id": 1, "user_id": kwargs["user_id"]}

    monkeypatch.setattr(apollo_api, "init_session_from_hoot", _fake_init)

    r = TestClient(app).post(
        "/apollo/sessions/from_hoot",
        headers={"Authorization": "Bearer tok"},
        json={"search_space_id": TEST_SPACE_ID, "hoot_transcript": "2+2=4"},
    )
    assert r.status_code == 200
    assert captured["user_id"] == TEST_USER_ID
    assert captured["search_space_id"] == TEST_SPACE_ID
