"""Real-Postgres contract tests for smart-question opportunity migration 042."""

from __future__ import annotations

import asyncio
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.integration

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "database/migrations/042_apollo_reference_question_opportunities.sql"
)
DB_NAME = "apollo_reference_question_opportunities_migration"


def _dsn(url: str, database: str) -> str:
    return (
        make_url(url)
        .set(drivername="postgresql", database=database)
        .render_as_string(hide_password=False)
    )


@pytest.fixture(scope="session")
def opportunity_dsn(_pg_url: str):
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
                "CREATE TABLE apollo_sessions (id BIGSERIAL PRIMARY KEY);"
                "CREATE TABLE apollo_problem_attempts (id BIGSERIAL PRIMARY KEY);"
            )
            await conn.execute(MIGRATION.read_text(encoding="utf-8"))
        finally:
            await conn.close()

    asyncio.run(setup())
    return test_dsn


@pytest_asyncio.fixture
async def opportunity_conn(opportunity_dsn):
    conn = await asyncpg.connect(opportunity_dsn)
    transaction = conn.transaction()
    await transaction.start()
    try:
        yield conn
    finally:
        await transaction.rollback()
        await conn.close()


async def _parents(conn):
    session_id = await conn.fetchval("INSERT INTO apollo_sessions DEFAULT VALUES RETURNING id")
    attempt_id = await conn.fetchval(
        "INSERT INTO apollo_problem_attempts DEFAULT VALUES RETURNING id"
    )
    return session_id, attempt_id


async def _insert(conn, session_id, attempt_id, node="n1", state="asked_waiting"):
    return await conn.fetchval(
        "INSERT INTO apollo_reference_question_opportunities "
        "(attempt_id, session_id, reference_node_id, state, question, asked_turn) "
        "VALUES ($1,$2,$3,$4,'Why?',1) RETURNING id",
        attempt_id,
        session_id,
        node,
        state,
    )


@pytest.mark.asyncio
async def test_unique_node_and_state_check(opportunity_conn):
    session_id, attempt_id = await _parents(opportunity_conn)
    await _insert(opportunity_conn, session_id, attempt_id)
    with pytest.raises(asyncpg.UniqueViolationError):
        await _insert(opportunity_conn, session_id, attempt_id)


@pytest.mark.asyncio
async def test_invalid_state_rejected(opportunity_conn):
    session_id, attempt_id = await _parents(opportunity_conn)
    with pytest.raises(asyncpg.CheckViolationError):
        await _insert(opportunity_conn, session_id, attempt_id, state="ask_again")


@pytest.mark.asyncio
async def test_attempt_delete_cascades_and_rls_is_enabled(opportunity_conn):
    session_id, attempt_id = await _parents(opportunity_conn)
    await _insert(opportunity_conn, session_id, attempt_id)
    await opportunity_conn.execute("DELETE FROM apollo_problem_attempts WHERE id=$1", attempt_id)
    assert (
        await opportunity_conn.fetchval(
            "SELECT count(*) FROM apollo_reference_question_opportunities"
        )
        == 0
    )
    assert (
        await opportunity_conn.fetchval(
            "SELECT relrowsecurity FROM pg_class WHERE relname='apollo_reference_question_opportunities'"
        )
        is True
    )
