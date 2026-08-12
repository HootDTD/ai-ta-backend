"""Independent, COMMITTING AsyncSessions on the shared Testcontainers Postgres.

The suite-wide ``db_session`` fixture (``tests/conftest.py``) hands out ONE
session on ONE connection inside an outer transaction that is rolled back at
teardown (``join_transaction_mode="create_savepoint"``). That is exactly wrong
for a concurrency test: two coroutines sharing it share a transaction, and
anything one of them "commits" is invisible to a second connection.

This fixture instead yields an ``async_sessionmaker`` over a NullPool engine on
the same container database, so every ``async with maker() as db`` is a REAL,
independent connection with REAL commits. Teardown deletes the courses the test
created (``app.courses`` cascades to learning_activities -> problem_attempts ->
question_opportunities/tutoring_messages, and to student_progress), leaving the
shared schema pristine for the rollback-based tests.
"""

from __future__ import annotations

import uuid

import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from database.models import Course

# Same connect args the shared harness uses: pgvector lives in ``extensions``
# and its SQLAlchemy type compiles unqualified.
_CONNECT_ARGS = {"server_settings": {"search_path": "public,extensions"}}


@pytest_asyncio.fixture
async def pg_committing_sessions(_pg_url):
    """Yield ``(session_factory, slug_prefix)``.

    Seed every ``Course`` the test creates with a slug starting with
    ``slug_prefix`` so teardown can find and cascade-delete it.
    """
    engine = create_async_engine(_pg_url, poolclass=NullPool, connect_args=_CONNECT_ARGS)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    slug_prefix = f"p34-{uuid.uuid4().hex[:12]}"
    try:
        yield maker, slug_prefix
    finally:
        async with maker() as cleanup:
            await cleanup.execute(delete(Course).where(Course.slug.like(f"{slug_prefix}%")))
            await cleanup.commit()
        await engine.dispose()
