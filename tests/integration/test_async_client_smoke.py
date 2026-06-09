"""Phase 1 exit criterion #2: an endpoint runs via AsyncClient on a rolled-back DB.

Demonstrates the integration-test machinery Phase 4 will apply to the real
`server.app` endpoints:

  - `httpx.AsyncClient` + `ASGITransport` (NOT starlette `TestClient`, which
    breaks on asyncpg's event loop),
  - the app's `get_db_session` dependency overridden to the transactional test
    session (via `override_db_session`),
  - writes made through the endpoint are visible within the test and then
    rolled back on teardown, leaving the database pristine.

A minimal inline app is used so the proof is deterministic and independent of
the heavy `server.py` import graph; the fixtures and override helper are the
reusable parts.
"""
from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import AITADocument
from database.session import get_db_session
from tests.factories import AITADocumentFactory, SearchSpaceFactory, persist
from tests.support import override_db_session

pytestmark = pytest.mark.integration


def _build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/doc-count")
    async def doc_count(session: AsyncSession = Depends(get_db_session)) -> dict:
        total = (
            await session.execute(select(func.count()).select_from(AITADocument))
        ).scalar_one()
        return {"count": total}

    return app


async def test_endpoint_sees_session_writes_then_rolls_back(db_session):
    app = _build_app()
    override_db_session(app, db_session)

    # Seed two documents through the SAME transactional session the endpoint uses.
    space = await persist(db_session, SearchSpaceFactory.build())
    for i in range(2):
        await persist(
            db_session,
            AITADocumentFactory.build(
                search_space_id=space.id,
                title=f"doc-{i}",
                content_hash=f"smoke-{i}",
            ),
        )
    await db_session.flush()

    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/doc-count")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json() == {"count": 2}
    # No commit happened; the db_session teardown rolls these rows back.


async def test_clean_database_starts_empty(db_session):
    """Confirms transactional isolation: the previous test's rows are gone."""
    total = (
        await db_session.execute(select(func.count()).select_from(AITADocument))
    ).scalar_one()
    assert total == 0
