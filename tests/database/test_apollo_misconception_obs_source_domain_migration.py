"""Real-Postgres test for the `source` domain CHECK on
``apollo_misconception_observations`` (migration 040).

Applies 037 then 040 verbatim to a fresh DB on the session pgvector container
and enumerates every source×promotable branch the DB test contract requires:
each of the three valid ``source`` values inserts cleanly, an out-of-domain
value raises a CheckViolationError, re-applying 040 is idempotent, and the
existing default-source insert path (mirroring
``record_observations_from_canonical``'s ``'grading_artifact'`` default)
still succeeds unchanged. Mirrors
``test_apollo_misconception_observations_migration.py`` /
``test_apollo_misconception_opposes_migration.py``.
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
MIGRATED_DB_NAME = "apollo_misconception_obs_source_domain_migrations"
MIGRATION_037 = MIGRATIONS_DIR / "037_apollo_misconception_observations.sql"
MIGRATION_040 = MIGRATIONS_DIR / "040_apollo_misconception_obs_source_domain.sql"

# 037 references aita_search_spaces, apollo_concepts, apollo_problem_attempts —
# none of which it creates. Stub minimal shapes (same convention as the 037 test).
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
            await conn.execute(MIGRATION_040.read_text(encoding="utf-8"))
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


async def _insert_obs(conn, *, space, concept, attempt, signature, source=None, user=None):
    if source is None:
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
    return await conn.fetchval(
        """
        INSERT INTO apollo_misconception_observations
            (search_space_id, concept_id, signature, user_id, attempt_id, confidence, source)
        VALUES ($1, $2, $3, $4, $5, 0.9, $6)
        RETURNING id
        """,
        space,
        concept,
        signature,
        user or uuid.uuid4(),
        attempt,
        source,
    )


@pytest.mark.asyncio
async def test_constraint_exists(mig_conn):
    conname = await mig_conn.fetchval(
        "SELECT conname FROM pg_constraint WHERE conname = 'ck_misconception_obs_source'"
    )
    assert conname == "ck_misconception_obs_source"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source", ["grading_artifact", "detector_unkeyed", "clarification_refuted"]
)
async def test_each_valid_source_inserts(mig_conn, source):
    space = await _new_space(mig_conn)
    concept = await _new_concept(mig_conn)
    attempt = await _new_attempt(mig_conn)
    obs_id = await _insert_obs(
        mig_conn,
        space=space,
        concept=concept,
        attempt=attempt,
        signature=f"sig.{source}",
        source=source,
    )
    assert obs_id is not None
    got = await mig_conn.fetchval(
        "SELECT source FROM apollo_misconception_observations WHERE id = $1", obs_id
    )
    assert got == source


@pytest.mark.asyncio
async def test_invalid_source_raises_check_violation(mig_conn):
    space = await _new_space(mig_conn)
    concept = await _new_concept(mig_conn)
    attempt = await _new_attempt(mig_conn)
    with pytest.raises(asyncpg.CheckViolationError):
        await _insert_obs(
            mig_conn,
            space=space,
            concept=concept,
            attempt=attempt,
            signature="sig.bogus",
            source="bogus",
        )


@pytest.mark.asyncio
async def test_default_source_insert_path_still_succeeds(mig_conn):
    # Mirrors record_observations_from_canonical's implicit 'grading_artifact'
    # default insert (no explicit source column) — must still succeed post-040.
    space = await _new_space(mig_conn)
    concept = await _new_concept(mig_conn)
    attempt = await _new_attempt(mig_conn)
    obs_id = await _insert_obs(
        mig_conn, space=space, concept=concept, attempt=attempt, signature="sig.default"
    )
    got = await mig_conn.fetchval(
        "SELECT source FROM apollo_misconception_observations WHERE id = $1", obs_id
    )
    assert got == "grading_artifact"


@pytest.mark.asyncio
async def test_reapplying_migration_is_idempotent(mig_conn):
    # Re-running 040's DDL body against the already-migrated schema must not error.
    await mig_conn.execute(MIGRATION_040.read_text(encoding="utf-8"))
    conname = await mig_conn.fetchval(
        "SELECT conname FROM pg_constraint WHERE conname = 'ck_misconception_obs_source'"
    )
    assert conname == "ck_misconception_obs_source"
