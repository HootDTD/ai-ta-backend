"""Real-Postgres contract test for Apollo grounding-bundle migration 048."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.integration

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "database/migrations/048_apollo_session_grounding_bundle.sql"
)
DB_NAME = "apollo_session_grounding_bundle_migration"


def _dsn(url: str, database: str) -> str:
    return (
        make_url(url)
        .set(drivername="postgresql", database=database)
        .render_as_string(hide_password=False)
    )


@pytest.fixture(scope="session")
def grounding_bundle_dsn(_pg_url: str):
    base = make_url(_pg_url).database
    assert base
    admin_dsn, test_dsn = _dsn(_pg_url, base), _dsn(_pg_url, DB_NAME)

    async def setup():
        admin = await asyncpg.connect(admin_dsn)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{DB_NAME}"')
            await admin.execute(f'CREATE DATABASE "{DB_NAME}"')
        finally:
            await admin.close()

        conn = await asyncpg.connect(test_dsn)
        try:
            await conn.execute(
                "CREATE SCHEMA app;CREATE TABLE app.learning_activities (id BIGSERIAL PRIMARY KEY);"
            )
            sql = MIGRATION.read_text(encoding="utf-8")
            await conn.execute(sql)
            await conn.execute(sql)
        finally:
            await conn.close()

    asyncio.run(setup())
    return test_dsn


@pytest_asyncio.fixture
async def grounding_bundle_conn(grounding_bundle_dsn):
    conn = await asyncpg.connect(grounding_bundle_dsn)
    transaction = conn.transaction()
    await transaction.start()
    try:
        yield conn
    finally:
        await transaction.rollback()
        await conn.close()


@pytest.mark.asyncio
async def test_nullable_jsonb_column_round_trip(grounding_bundle_conn):
    column = await grounding_bundle_conn.fetchrow(
        """
        SELECT data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'app'
          AND table_name = 'learning_activities'
          AND column_name = 'grounding_bundle'
        """
    )
    assert dict(column) == {"data_type": "jsonb", "is_nullable": "YES"}

    empty_id = await grounding_bundle_conn.fetchval(
        "INSERT INTO app.learning_activities DEFAULT VALUES RETURNING id"
    )
    assert (
        await grounding_bundle_conn.fetchval(
            "SELECT grounding_bundle FROM app.learning_activities WHERE id = $1",
            empty_id,
        )
        is None
    )

    payload = {
        "snippets": [{"id": "chunk-1", "text": "Bernoulli evidence"}],
        "diag": {"hit_count_raw": 1},
        "built_at": "2026-07-26T12:00:00+00:00",
        "retrieval_version": "retrieve_for_question:v1",
    }
    stored = await grounding_bundle_conn.fetchval(
        """
        INSERT INTO app.learning_activities (grounding_bundle)
        VALUES ($1::jsonb)
        RETURNING grounding_bundle
        """,
        json.dumps(payload),
    )
    assert json.loads(stored) == payload
