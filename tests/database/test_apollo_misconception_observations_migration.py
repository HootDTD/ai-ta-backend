"""Real-Postgres tests for ``apollo_misconception_observations`` (migration 037).

Applies migration 037 verbatim to a fresh database on the session pgvector
container and exercises the UNIQUE(attempt_id, signature) idempotency guard and
the FK ON DELETE behaviors (search_space CASCADE, concept/attempt SET NULL) for
real — matching the migration-application pattern in
``test_apollo_grading_artifacts_constraints.py``.
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
MIGRATED_DB_NAME = "apollo_misconception_obs_migrations"
MIGRATION_037 = MIGRATIONS_DIR / "037_apollo_misconception_observations.sql"

# 037 references aita_search_spaces, apollo_concepts, apollo_problem_attempts —
# none of which it creates. Stub minimal shapes (same convention as the 034 test).
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
            await conn.execute(MIGRATION_037.read_text(encoding="utf-8"))
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


async def _new_space(conn: asyncpg.Connection) -> int:
    return await conn.fetchval("INSERT INTO aita_search_spaces DEFAULT VALUES RETURNING id")


async def _new_concept(conn: asyncpg.Connection) -> int:
    return await conn.fetchval("INSERT INTO apollo_concepts DEFAULT VALUES RETURNING id")


async def _new_attempt(conn: asyncpg.Connection) -> int:
    return await conn.fetchval("INSERT INTO apollo_problem_attempts DEFAULT VALUES RETURNING id")


async def _insert_obs(conn, *, space, concept, attempt, signature, user=None):
    return await conn.fetchval(
        """
        INSERT INTO apollo_misconception_observations
            (search_space_id, concept_id, signature, user_id, attempt_id, confidence)
        VALUES ($1, $2, $3, $4, $5, 0.9)
        RETURNING id
        """,
        space,
        concept,
        signature,
        user or uuid.uuid4(),
        attempt,
    )


@pytest.mark.asyncio
async def test_table_and_index_exist(mig_conn):
    assert await mig_conn.fetchval(
        "SELECT to_regclass('public.apollo_misconception_observations') IS NOT NULL"
    )
    idx = await mig_conn.fetchval(
        "SELECT indexname FROM pg_indexes WHERE indexname = "
        "'ix_apollo_misconception_obs_space_concept'"
    )
    assert idx == "ix_apollo_misconception_obs_space_concept"


@pytest.mark.asyncio
async def test_unique_attempt_signature_rejects_duplicate(mig_conn):
    space = await _new_space(mig_conn)
    concept = await _new_concept(mig_conn)
    attempt = await _new_attempt(mig_conn)
    await _insert_obs(mig_conn, space=space, concept=concept, attempt=attempt, signature="misc.a")
    with pytest.raises(asyncpg.UniqueViolationError):
        await _insert_obs(
            mig_conn, space=space, concept=concept, attempt=attempt, signature="misc.a"
        )


@pytest.mark.asyncio
async def test_same_attempt_distinct_signatures_allowed(mig_conn):
    space = await _new_space(mig_conn)
    concept = await _new_concept(mig_conn)
    attempt = await _new_attempt(mig_conn)
    id1 = await _insert_obs(
        mig_conn, space=space, concept=concept, attempt=attempt, signature="misc.a"
    )
    id2 = await _insert_obs(
        mig_conn, space=space, concept=concept, attempt=attempt, signature="misc.b"
    )
    assert id1 != id2


@pytest.mark.asyncio
async def test_search_space_delete_cascades(mig_conn):
    space = await _new_space(mig_conn)
    concept = await _new_concept(mig_conn)
    attempt = await _new_attempt(mig_conn)
    await _insert_obs(mig_conn, space=space, concept=concept, attempt=attempt, signature="misc.a")
    await mig_conn.execute("DELETE FROM aita_search_spaces WHERE id = $1", space)
    remaining = await mig_conn.fetchval(
        "SELECT count(*) FROM apollo_misconception_observations WHERE search_space_id = $1", space
    )
    assert remaining == 0


@pytest.mark.asyncio
async def test_concept_and_attempt_delete_set_null(mig_conn):
    space = await _new_space(mig_conn)
    concept = await _new_concept(mig_conn)
    attempt = await _new_attempt(mig_conn)
    obs_id = await _insert_obs(
        mig_conn, space=space, concept=concept, attempt=attempt, signature="misc.a"
    )
    await mig_conn.execute("DELETE FROM apollo_concepts WHERE id = $1", concept)
    await mig_conn.execute("DELETE FROM apollo_problem_attempts WHERE id = $1", attempt)
    row = await mig_conn.fetchrow(
        "SELECT concept_id, attempt_id, search_space_id "
        "FROM apollo_misconception_observations WHERE id = $1",
        obs_id,
    )
    assert row["concept_id"] is None
    assert row["attempt_id"] is None
    assert row["search_space_id"] == space  # space FK untouched


@pytest.mark.asyncio
async def test_source_defaults_to_grading_artifact(mig_conn):
    space = await _new_space(mig_conn)
    concept = await _new_concept(mig_conn)
    attempt = await _new_attempt(mig_conn)
    obs_id = await _insert_obs(
        mig_conn, space=space, concept=concept, attempt=attempt, signature="misc.a"
    )
    source = await mig_conn.fetchval(
        "SELECT source FROM apollo_misconception_observations WHERE id = $1", obs_id
    )
    assert source == "grading_artifact"
