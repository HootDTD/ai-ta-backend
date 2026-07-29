from __future__ import annotations

import pytest
from sqlalchemy import select, text

from database import session as db_session_mod
from database.models import Course


def test_build_engine_sets_pool_recycle(monkeypatch):
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql+asyncpg://u:p@localhost:5432/db")
    engine = db_session_mod._build_engine()
    # pool_recycle is surfaced on the sync pool behind the async engine.
    assert engine.sync_engine.pool._recycle == 1800
    # pool_pre_ping is surfaced via engine.sync_engine.pool._pre_ping (SQLAlchemy 2.x).
    assert engine.sync_engine.pool._pre_ping is True


def test_build_engine_translates_target_schemas_for_sqlite(monkeypatch):
    monkeypatch.setenv("SUPABASE_DB_URL", "sqlite+aiosqlite:///:memory:")

    engine = db_session_mod._build_engine()

    assert engine.get_execution_options()["schema_translate_map"] == {
        "app": None,
        "internal": None,
    }


@pytest.mark.asyncio
async def test_sqlite_engine_queries_schema_qualified_model(monkeypatch):
    monkeypatch.setenv("SUPABASE_DB_URL", "sqlite+aiosqlite:///:memory:")
    engine = db_session_mod._build_engine()
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE courses (id INTEGER PRIMARY KEY)"))
        await connection.execute(text("INSERT INTO courses (id) VALUES (7)"))
        assert (await connection.execute(select(Course.id))).scalar_one() == 7
    await engine.dispose()
