from __future__ import annotations

from database import session as db_session_mod


def test_build_engine_sets_pool_recycle(monkeypatch):
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql+asyncpg://u:p@localhost:5432/db")
    engine = db_session_mod._build_engine()
    # pool_recycle is surfaced on the sync pool behind the async engine.
    assert engine.sync_engine.pool._recycle == 1800
    # pool_pre_ping is surfaced via engine.sync_engine.pool._pre_ping (SQLAlchemy 2.x).
    assert engine.sync_engine.pool._pre_ping is True
