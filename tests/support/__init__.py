"""Reusable test harness helpers (Phase 1, docs/TESTING-CI-PLAN.md)."""

from __future__ import annotations

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession


def override_db_session(app: FastAPI, session: AsyncSession) -> None:
    """Point a FastAPI app's `get_db_session` dependency at a test session.

    Reusable by integration/e2e tests that drive the real app through
    ``httpx.AsyncClient`` + ``ASGITransport``. Pair with
    ``app.dependency_overrides.clear()`` in teardown.
    """
    from database.session import get_db_session

    async def _override():
        yield session

    app.dependency_overrides[get_db_session] = _override


__all__ = ["override_db_session"]
