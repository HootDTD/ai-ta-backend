"""Real-Postgres test for apollo_misconceptions.opposes (migration 038).

Applies 019 then 038 verbatim to a fresh DB on the session pgvector container
and asserts the opposes column exists, is nullable, defaults NULL, and accepts a
value — enumerating the column's add/nullable/insert behaviors per the DB test
contract. Mirrors test_apollo_misconception_observations_migration.py.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.integration

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "database" / "migrations"
MIGRATED_DB_NAME = "apollo_misconception_opposes_migrations"
MIGRATION_019 = MIGRATIONS_DIR / "019_apollo_misconceptions.sql"
MIGRATION_038 = MIGRATIONS_DIR / "038_apollo_misconception_opposes.sql"

# 019 references apollo_concepts + needs pgvector; stub the concept parent and
# create the extension (the container image ships pgvector).
_STUB_DDL = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE apollo_concepts (id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY);
"""


def _plain_dsn(sqlalchemy_url: str, database: str) -> str:
    url = make_url(sqlalchemy_url).set(drivername="postgresql", database=database)
    return url.render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def _migrated_dsn(_pg_url: str):
    base_db = make_url(_pg_url).database
    assert base_db
    admin_dsn = _plain_dsn(_pg_url, base_db)
    migrated_dsn = _plain_dsn(_pg_url, MIGRATED_DB_NAME)

    async def _setup() -> None:
        admin = await asyncpg.connect(admin_dsn)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{MIGRATED_DB_NAME}"')
            await admin.execute(f'CREATE DATABASE "{MIGRATED_DB_NAME}"')
        finally:
            await admin.close()
        conn = await asyncpg.connect(migrated_dsn)
        try:
            await conn.execute(_STUB_DDL)
            await conn.execute(MIGRATION_019.read_text(encoding="utf-8"))
            await conn.execute(MIGRATION_038.read_text(encoding="utf-8"))
        finally:
            await conn.close()

    asyncio.run(_setup())
    yield migrated_dsn


@pytest_asyncio.fixture
async def mig_conn(_migrated_dsn: str):
    conn = await asyncpg.connect(_migrated_dsn)
    tr = conn.transaction()
    await tr.start()
    try:
        yield conn
    finally:
        await tr.rollback()
        await conn.close()


@pytest.mark.asyncio
async def test_opposes_column_exists_and_is_nullable(mig_conn) -> None:
    row = await mig_conn.fetchrow(
        """
        SELECT data_type, is_nullable
        FROM information_schema.columns
        WHERE table_name = 'apollo_misconceptions' AND column_name = 'opposes'
        """
    )
    assert row is not None, "opposes column missing"
    assert row["data_type"] == "text"
    assert row["is_nullable"] == "YES"


@pytest.mark.asyncio
async def test_opposes_defaults_null_and_accepts_value(mig_conn) -> None:
    await mig_conn.execute("INSERT INTO apollo_concepts DEFAULT VALUES")
    cid = await mig_conn.fetchval("SELECT id FROM apollo_concepts LIMIT 1")
    # NULL (default) row.
    await mig_conn.execute(
        "INSERT INTO apollo_misconceptions (concept_id, code, description, probe_question) "
        "VALUES ($1, 'm1', 'd', 'p')",
        cid,
    )
    assert (
        await mig_conn.fetchval("SELECT opposes FROM apollo_misconceptions WHERE code = 'm1'")
        is None
    )
    # Explicit value row.
    await mig_conn.execute(
        "INSERT INTO apollo_misconceptions (concept_id, code, description, probe_question, opposes) "
        "VALUES ($1, 'm2', 'd', 'p', 'def.real_basis')",
        cid,
    )
    assert (
        await mig_conn.fetchval("SELECT opposes FROM apollo_misconceptions WHERE code = 'm2'")
        == "def.real_basis"
    )
