"""Real-Postgres contract tests for Apollo question-tally migration 047."""

from __future__ import annotations

import asyncio
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.integration

MIGRATION = (
    Path(__file__).resolve().parents[2] / "database/migrations/047_apollo_question_tally.sql"
)
DB_NAME = "apollo_question_tally_migration"


def _dsn(url: str, database: str) -> str:
    return (
        make_url(url)
        .set(drivername="postgresql", database=database)
        .render_as_string(hide_password=False)
    )


@pytest.fixture(scope="session")
def tally_dsn(_pg_url: str):
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
            await conn.execute("CREATE TABLE apollo_problem_attempts (id BIGSERIAL PRIMARY KEY);")
            await conn.execute(MIGRATION.read_text(encoding="utf-8"))
        finally:
            await conn.close()

    asyncio.run(setup())
    return test_dsn


@pytest_asyncio.fixture
async def tally_conn(tally_dsn):
    conn = await asyncpg.connect(tally_dsn)
    transaction = conn.transaction()
    await transaction.start()
    try:
        yield conn
    finally:
        await transaction.rollback()
        await conn.close()


async def _attempt(conn):
    return await conn.fetchval("INSERT INTO apollo_problem_attempts DEFAULT VALUES RETURNING id")


@pytest.mark.asyncio
async def test_defaults_unique_round_trip_and_rls(tally_conn):
    attempt_id = await _attempt(tally_conn)
    row = await tally_conn.fetchrow(
        "INSERT INTO apollo_question_tally (attempt_id, reference_node_id, status) "
        "VALUES ($1, 'n1', 'missing') RETURNING *",
        attempt_id,
    )
    assert row["evidence"] == "[]"
    assert row["student_declined"] is False
    assert row["times_asked"] == 0
    assert (
        await tally_conn.fetchval(
            "SELECT relrowsecurity FROM pg_class WHERE relname='apollo_question_tally'"
        )
        is True
    )
    with pytest.raises(asyncpg.UniqueViolationError):
        await tally_conn.execute(
            "INSERT INTO apollo_question_tally (attempt_id, reference_node_id, status) "
            "VALUES ($1, 'n1', 'understood')",
            attempt_id,
        )


@pytest.mark.asyncio
async def test_attempt_delete_cascades(tally_conn):
    attempt_id = await _attempt(tally_conn)
    await tally_conn.execute(
        "INSERT INTO apollo_question_tally "
        "(attempt_id, reference_node_id, status, evidence, student_declined, times_asked, last_asked_turn) "
        "VALUES ($1, 'n1', 'tentative', $2::jsonb, true, 2, 5)",
        attempt_id,
        '[{"turn_id": 4, "quote": "not sure"}]',
    )
    await tally_conn.execute("DELETE FROM apollo_problem_attempts WHERE id=$1", attempt_id)
    assert await tally_conn.fetchval("SELECT count(*) FROM apollo_question_tally") == 0
