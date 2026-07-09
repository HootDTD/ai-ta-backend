"""Real-Postgres tests for the ``apollo_grading_artifacts`` CHECK constraints
and unique index, applying the actual migration 034 SQL.

The model layer declares no CHECK constraints (repo convention — see
``test_apollo_attempt_result_constraint.py``), so the ``db_session`` harness
(schema from ``Base.metadata.create_all``) can never catch a ``role``/
``grader_used`` CHECK violation. These tests apply migration 034 verbatim to a
fresh database on the session pgvector container and exercise the constraints
for real, matching the migration-application pattern used for the
``apollo_problem_attempts.result`` constraint.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.integration

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "database" / "migrations"
MIGRATED_DB_NAME = "apollo_grading_artifacts_migrations"
MIGRATION_034 = MIGRATIONS_DIR / "034_apollo_grading_artifacts.sql"

# 034 only needs the FK targets it references — apollo_problem_attempts,
# aita_search_spaces, apollo_concepts — none of which it creates itself.
# Stub minimal shapes exactly like test_apollo_attempt_result_constraint.py
# and test_apollo_learner_model_migration.py do for the same tables.
_STUB_DDL = """
CREATE TABLE aita_search_spaces (id SERIAL PRIMARY KEY);
CREATE TABLE apollo_concepts (id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY);
CREATE TABLE apollo_problem_attempts (id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY);
"""


def _plain_dsn(sqlalchemy_url: str, database: str) -> str:
    url = make_url(sqlalchemy_url).set(drivername="postgresql", database=database)
    return url.render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def _migrated_dsn(_pg_url: str):
    """Create a fresh DB on the session container and apply 034 verbatim."""
    base_db = make_url(_pg_url).database
    assert base_db, "container URL has no database name"
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
            await conn.execute(MIGRATION_034.read_text(encoding="utf-8"))
        finally:
            await conn.close()

    asyncio.run(_setup())
    yield migrated_dsn


@pytest_asyncio.fixture
async def mig_conn(_migrated_dsn: str):
    """Connection to the migrated DB; everything rolls back after each test."""
    conn = await asyncpg.connect(_migrated_dsn)
    tr = conn.transaction()
    await tr.start()
    try:
        yield conn
    finally:
        await tr.rollback()
        await conn.close()


async def _new_attempt(conn: asyncpg.Connection) -> int:
    return await conn.fetchval("INSERT INTO apollo_problem_attempts DEFAULT VALUES RETURNING id")


async def _new_space(conn: asyncpg.Connection) -> int:
    return await conn.fetchval("INSERT INTO aita_search_spaces DEFAULT VALUES RETURNING id")


async def _insert_artifact(
    conn: asyncpg.Connection,
    *,
    attempt_id: int,
    space_id: int,
    role: str,
    grader_used: str,
) -> int:
    return await conn.fetchval(
        """
        INSERT INTO apollo_grading_artifacts
            (attempt_id, role, grader_used, user_id, search_space_id, problem_id,
             versions, node_ledger, edge_ledger, scores)
        VALUES ($1, $2, $3, $4, $5, 'p1', '{}'::jsonb, '[]'::jsonb, '[]'::jsonb, '{}'::jsonb)
        RETURNING id
        """,
        attempt_id,
        role,
        grader_used,
        uuid.uuid4(),
        space_id,
    )


@pytest.mark.parametrize("role", ["canonical", "pair"])
@pytest.mark.parametrize("grader_used", ["graph", "llm_fallback"])
async def test_valid_role_and_grader_values_persist(mig_conn, role, grader_used):
    attempt_id = await _new_attempt(mig_conn)
    space_id = await _new_space(mig_conn)
    artifact_id = await _insert_artifact(
        mig_conn, attempt_id=attempt_id, space_id=space_id, role=role, grader_used=grader_used
    )
    assert artifact_id is not None


async def test_invalid_role_rejected(mig_conn):
    attempt_id = await _new_attempt(mig_conn)
    space_id = await _new_space(mig_conn)
    with pytest.raises(asyncpg.CheckViolationError):
        await _insert_artifact(
            mig_conn, attempt_id=attempt_id, space_id=space_id, role="bogus", grader_used="graph"
        )


async def test_invalid_grader_used_rejected(mig_conn):
    attempt_id = await _new_attempt(mig_conn)
    space_id = await _new_space(mig_conn)
    with pytest.raises(asyncpg.CheckViolationError):
        await _insert_artifact(
            mig_conn,
            attempt_id=attempt_id,
            space_id=space_id,
            role="canonical",
            grader_used="bogus",
        )


async def test_unique_attempt_role_enforced(mig_conn):
    attempt_id = await _new_attempt(mig_conn)
    space_id = await _new_space(mig_conn)
    await _insert_artifact(
        mig_conn, attempt_id=attempt_id, space_id=space_id, role="canonical", grader_used="graph"
    )
    with pytest.raises(asyncpg.UniqueViolationError):
        await _insert_artifact(
            mig_conn,
            attempt_id=attempt_id,
            space_id=space_id,
            role="canonical",
            grader_used="llm_fallback",
        )
