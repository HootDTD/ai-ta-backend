"""Unit tests for campaign.infra.apply_migrations.

migration_files() ordering/duplicate-detection is pure and tested directly.
apply_all() is tested against a fake asyncpg-like connection so no Docker/DB
is required; the real asyncpg path is exercised manually (Task C1 Step 2,
campaign/README.md) against the local Supabase stack.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from campaign.infra.apply_migrations import (
    MigrationOrderError,
    apply_all,
    bootstrap_baseline,
    migration_files,
    to_asyncpg_dsn,
    to_sqlalchemy_dsn,
)

pytestmark = pytest.mark.unit


def _touch(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_text(f"-- {name}\nSELECT 1;\n", encoding="utf-8")
    return path


def test_migration_files_sorts_numerically_not_lexically(tmp_path):
    _touch(tmp_path, "010_ten.sql")
    _touch(tmp_path, "002_two.sql")
    _touch(tmp_path, "001_one.sql")

    files = migration_files(tmp_path)

    assert [f.name for f in files] == ["001_one.sql", "002_two.sql", "010_ten.sql"]


def test_migration_files_allows_known_023_duplicate(tmp_path):
    _touch(tmp_path, "023_apollo_auth_scoping.sql")
    _touch(tmp_path, "023_chunks_halfvec_hnsw.sql")
    _touch(tmp_path, "024_teacher_textbook.sql")

    files = migration_files(tmp_path)

    # Stable secondary sort by filename for the known duplicate pair.
    assert [f.name for f in files] == [
        "023_apollo_auth_scoping.sql",
        "023_chunks_halfvec_hnsw.sql",
        "024_teacher_textbook.sql",
    ]


def test_migration_files_rejects_unknown_duplicate_number(tmp_path):
    _touch(tmp_path, "005_a.sql")
    _touch(tmp_path, "005_b.sql")

    with pytest.raises(MigrationOrderError, match="duplicate migration number 5"):
        migration_files(tmp_path)


def test_migration_files_rejects_bad_filename(tmp_path):
    (tmp_path / "not_a_migration.sql").write_text("SELECT 1;", encoding="utf-8")

    with pytest.raises(MigrationOrderError, match="does not match"):
        migration_files(tmp_path)


def test_migration_files_ignores_non_sql(tmp_path):
    _touch(tmp_path, "001_one.sql")
    (tmp_path / "README.md").write_text("not sql", encoding="utf-8")

    files = migration_files(tmp_path)

    assert [f.name for f in files] == ["001_one.sql"]


class _FakeConn:
    """Minimal stand-in for an asyncpg connection used by apply_all."""

    def __init__(self, already_applied: set[str] | None = None):
        self.applied_rows: set[str] = set(already_applied or set())
        self.executed_sql: list[str] = []
        self.closed = False

    async def execute(self, sql, *args):
        self.executed_sql.append(sql)
        if sql.strip().startswith("INSERT INTO _campaign_migrations"):
            self.applied_rows.add(args[0])

    async def fetch(self, sql):
        assert "_campaign_migrations" in sql
        return [{"name": name} for name in sorted(self.applied_rows)]

    def transaction(self):
        return _FakeTransaction()

    async def close(self):
        self.closed = True


class _FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


@pytest.mark.asyncio
async def test_apply_all_applies_every_file_in_order(tmp_path):
    _touch(tmp_path, "001_one.sql")
    _touch(tmp_path, "002_two.sql")
    conn = _FakeConn()

    async def _connect(dsn):
        return conn

    applied = await apply_all("dsn://ignored", tmp_path, connect=_connect)

    assert applied == ["001_one.sql", "002_two.sql"]
    assert conn.applied_rows == {"001_one.sql", "002_two.sql"}
    assert conn.closed


@pytest.mark.asyncio
async def test_apply_all_skips_already_applied(tmp_path):
    _touch(tmp_path, "001_one.sql")
    _touch(tmp_path, "002_two.sql")
    conn = _FakeConn(already_applied={"001_one.sql"})

    async def _connect(dsn):
        return conn

    applied = await apply_all("dsn://ignored", tmp_path, connect=_connect)

    assert applied == ["002_two.sql"]


@pytest.mark.asyncio
async def test_apply_all_no_new_migrations_returns_empty(tmp_path):
    _touch(tmp_path, "001_one.sql")
    conn = _FakeConn(already_applied={"001_one.sql"})

    async def _connect(dsn):
        return conn

    applied = await apply_all("dsn://ignored", tmp_path, connect=_connect)

    assert applied == []


def test_to_asyncpg_dsn_strips_driver_marker():
    assert (
        to_asyncpg_dsn("postgresql+asyncpg://u:p@h:5432/db")
        == "postgresql://u:p@h:5432/db"
    )
    # Already-plain DSNs pass through unchanged.
    assert to_asyncpg_dsn("postgresql://u:p@h:5432/db") == "postgresql://u:p@h:5432/db"


def test_to_sqlalchemy_dsn_adds_driver_marker():
    assert (
        to_sqlalchemy_dsn("postgresql://u:p@h:5432/db")
        == "postgresql+asyncpg://u:p@h:5432/db"
    )
    # Already-annotated DSNs pass through unchanged.
    assert (
        to_sqlalchemy_dsn("postgresql+asyncpg://u:p@h:5432/db")
        == "postgresql+asyncpg://u:p@h:5432/db"
    )


@pytest.mark.asyncio
async def test_bootstrap_baseline_creates_extension_and_metadata(monkeypatch):
    """bootstrap_baseline drives a SQLAlchemy engine — verify the call shape
    (extension DDL + create_all) without a real DB by faking the engine."""
    calls: list[str] = []

    class _FakeSAConn:
        async def execute(self, clause):
            calls.append(str(clause))

        async def run_sync(self, fn):
            calls.append(f"run_sync:{fn.__name__ if hasattr(fn, '__name__') else fn}")

    class _FakeEngineCtx:
        async def __aenter__(self):
            return _FakeSAConn()

        async def __aexit__(self, *exc):
            return False

    class _FakeEngine:
        def begin(self):
            return _FakeEngineCtx()

        async def dispose(self):
            calls.append("disposed")

    def _fake_create_async_engine(dsn, poolclass=None):
        calls.append(f"engine:{dsn}")
        return _FakeEngine()

    import campaign.infra.apply_migrations as mod

    monkeypatch.setattr(
        "sqlalchemy.ext.asyncio.create_async_engine", _fake_create_async_engine
    )

    await bootstrap_baseline("postgresql://u:p@h:5432/db")

    assert any(c.startswith("engine:postgresql+asyncpg://") for c in calls)
    assert any("CREATE EXTENSION IF NOT EXISTS vector" in c for c in calls)
    assert any(c.startswith("run_sync:") for c in calls)
    assert "disposed" in calls
