"""Real-Postgres tests for the ``teacher_uploads`` CHECK constraints.

The textbook upload path writes ``week=0`` (``COURSE_WIDE_WEEK``) and
``kind='textbook'`` — values the original migration 004 constraints reject.
The model layer declares no CHECK constraints, so the ``db_session`` harness
(schema from ``Base.metadata.create_all``) can never catch a constraint
violation; these tests apply the actual migration SQL to a fresh database on
the session pgvector container and exercise the constraints for real.

Migration selection is content-based: every migration that creates or alters
``teacher_uploads`` joins the chain automatically, so a future constraint
change is tested without editing this file.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.integration

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "database" / "migrations"
MIGRATED_DB_NAME = "teacher_uploads_migrations"

# FK targets referenced by 004 that earlier migrations don't create (the base
# tables come from the app's model metadata in prod; auth.users is Supabase's).
_STUB_DDL = """
CREATE SCHEMA IF NOT EXISTS auth;
CREATE TABLE auth.users (id UUID PRIMARY KEY);
CREATE TABLE aita_search_spaces (id SERIAL PRIMARY KEY);
CREATE TABLE aita_documents (id SERIAL PRIMARY KEY);
"""

_TOUCHES_TEACHER_UPLOADS = re.compile(
    r"(CREATE TABLE IF NOT EXISTS|CREATE TABLE|ALTER TABLE)\s+teacher_uploads\b"
)


def _teacher_uploads_migrations() -> list[Path]:
    """Every migration that defines or rewrites the teacher_uploads schema."""
    return [
        path
        for path in sorted(MIGRATIONS_DIR.glob("*.sql"))
        if _TOUCHES_TEACHER_UPLOADS.search(path.read_text(encoding="utf-8"))
    ]


def _plain_dsn(sqlalchemy_url: str, database: str) -> str:
    url = make_url(sqlalchemy_url).set(drivername="postgresql", database=database)
    return url.render_as_string(hide_password=False)


@pytest.fixture(scope="session")
def _migrated_dsn(_pg_url: str):
    """Create a fresh DB on the session container and run the migration chain."""
    admin_dsn = _plain_dsn(_pg_url, make_url(_pg_url).database)
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
            for migration in _teacher_uploads_migrations():
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


async def _new_search_space(conn: asyncpg.Connection) -> int:
    return await conn.fetchval(
        "INSERT INTO aita_search_spaces DEFAULT VALUES RETURNING id"
    )


async def _insert_upload(
    conn: asyncpg.Connection,
    space_id: int,
    *,
    week: int,
    kind: str,
    is_latest: bool = True,
) -> int:
    return await conn.fetchval(
        """
        INSERT INTO teacher_uploads (search_space_id, week, kind, title, is_latest, status)
        VALUES ($1, $2, $3, 'Course Textbook', $4, 'queued')
        RETURNING id
        """,
        space_id,
        week,
        kind,
        is_latest,
    )


async def test_textbook_week0_insert_persists(mig_conn):
    """The exact row the textbook upload path writes must satisfy the schema."""
    space_id = await _new_search_space(mig_conn)

    upload_id = await _insert_upload(mig_conn, space_id, week=0, kind="textbook")

    row = await mig_conn.fetchrow(
        "SELECT week, kind, status FROM teacher_uploads WHERE id = $1", upload_id
    )
    assert (row["week"], row["kind"], row["status"]) == (0, "textbook", "queued")


async def test_unknown_kind_still_rejected(mig_conn):
    """Relaxing the kind check must not drop it entirely."""
    space_id = await _new_search_space(mig_conn)

    with pytest.raises(asyncpg.CheckViolationError):
        await _insert_upload(mig_conn, space_id, week=1, kind="exam")


async def test_week_out_of_range_still_rejected(mig_conn):
    """Relaxing the week check to include 0 must keep the upper bound."""
    space_id = await _new_search_space(mig_conn)

    with pytest.raises(asyncpg.CheckViolationError):
        await _insert_upload(mig_conn, space_id, week=17, kind="notes")


async def test_one_latest_textbook_per_course(mig_conn):
    """uniq_teacher_uploads_latest stays correct with the week=0 sentinel."""
    space_id = await _new_search_space(mig_conn)
    await _insert_upload(mig_conn, space_id, week=0, kind="textbook")

    with pytest.raises(asyncpg.UniqueViolationError):
        await _insert_upload(mig_conn, space_id, week=0, kind="textbook")
