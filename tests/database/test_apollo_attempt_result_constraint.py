"""Real-Postgres tests for the ``apollo_problem_attempts.result`` CHECK constraint.

handle_done writes ``result='graded'`` and handle_next writes
``result='abandoned'`` — values the original migration 009 constraint
(``'solved','stuck','skipped','returned_to_hoot'``) rejected, so every Apollo
"I'm finished teaching" raised asyncpg CheckViolationError -> HTTP 500 on
staging (2026-06-15) after the grade had already been computed.

The model layer declares no CHECK constraints, so the ``db_session`` harness
(schema from ``Base.metadata.create_all``) can never catch a constraint
violation — which is exactly why no test caught this. These tests apply the
actual migration SQL to a fresh database on the session pgvector container and
exercise the constraint for real, so any future drift between the result values
the code writes and the values the migrations permit fails the build.

Migration selection is content-based (same approach as
test_teacher_uploads_constraints): every migration that creates or alters
``apollo_problem_attempts`` joins the chain automatically, so a future
constraint change is covered without editing this file.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy.engine import make_url

from apollo.persistence.models import ATTEMPT_RESULTS

pytestmark = pytest.mark.integration

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "database" / "migrations"
MIGRATED_DB_NAME = "apollo_attempts_migrations"

# 009 creates apollo_sessions (self-contained — no external FK) alongside
# apollo_problem_attempts, so the content-scoped chain needs no FK stubs.
_TOUCHES_ATTEMPTS = re.compile(
    r"(CREATE TABLE IF NOT EXISTS|CREATE TABLE|ALTER TABLE)\s+apollo_problem_attempts\b"
)


def _attempt_migrations() -> list[Path]:
    """Every migration that defines or rewrites the apollo_problem_attempts schema."""
    return [
        path
        for path in sorted(MIGRATIONS_DIR.glob("*.sql"))
        if _TOUCHES_ATTEMPTS.search(path.read_text(encoding="utf-8"))
    ]


def _plain_dsn(sqlalchemy_url: str, database: str) -> str:
    url = make_url(sqlalchemy_url).set(drivername="postgresql", database=database)
    return url.render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def _migrated_dsn(_pg_url: str):
    """Create a fresh DB on the session container and run the migration chain."""
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
            for migration in _attempt_migrations():
                await conn.execute(migration.read_text(encoding="utf-8"))
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


async def _new_session(conn: asyncpg.Connection) -> int:
    return await conn.fetchval(
        """
        INSERT INTO apollo_sessions (student_id, concept_cluster_id)
        VALUES ('stu-1', 'fluid_mechanics')
        RETURNING id
        """
    )


async def _insert_attempt(conn: asyncpg.Connection, session_id: int, result: str | None) -> int:
    return await conn.fetchval(
        """
        INSERT INTO apollo_problem_attempts (session_id, problem_id, difficulty, result)
        VALUES ($1, 'bernoulli_horizontal_pipe_find_p2', 'intro', $2)
        RETURNING id
        """,
        session_id,
        result,
    )


@pytest.mark.parametrize("result", ["graded", "abandoned"])
async def test_code_written_results_persist(mig_conn, result):
    """The exact values handle_done ('graded') / handle_next ('abandoned') write
    must satisfy the migrated schema — this is the regression that 500'd."""
    session_id = await _new_session(mig_conn)
    attempt_id = await _insert_attempt(mig_conn, session_id, result)
    stored = await mig_conn.fetchval(
        "SELECT result FROM apollo_problem_attempts WHERE id = $1", attempt_id
    )
    assert stored == result


@pytest.mark.parametrize("result", list(ATTEMPT_RESULTS) + [None])
async def test_all_allowlisted_results_persist(mig_conn, result):
    """Every value in the app-layer allowlist must be DB-legal after migration 025."""
    session_id = await _new_session(mig_conn)
    await _insert_attempt(mig_conn, session_id, result)


async def test_unknown_result_still_rejected(mig_conn):
    """Widening the allowlist must not drop the constraint entirely."""
    session_id = await _new_session(mig_conn)
    with pytest.raises(asyncpg.CheckViolationError):
        await _insert_attempt(mig_conn, session_id, "not_a_real_result")
