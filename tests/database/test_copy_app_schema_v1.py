"""Local-Docker contract test for the DB-05 copy and reverse delta."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.integration

_REPO = Path(__file__).resolve().parents[2]
_MIGRATIONS = _REPO / "supabase" / "migrations"
_SNAPSHOT = _MIGRATIONS / "20260717032246_legacy_public_snapshot.sql"
_CREATE = _MIGRATIONS / "20260717035041_create_app_schema_v1.sql"
_COPY = _MIGRATIONS / "20260717043000_copy_app_schema_v1.sql"
_SEED = Path(__file__).parent / "fixtures" / "db05_legacy_seed.sql"
_RECONCILE = _REPO / "scripts" / "db" / "reconcile_copy.sql"
_REVERSE = _REPO / "scripts" / "db" / "rollback_reverse_copy.sql"
_DB_NAME = "copy_app_schema_v1"

_AUTH_BOOTSTRAP = """
DO $roles$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    CREATE ROLE anon NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
    CREATE ROLE authenticated NOLOGIN NOBYPASSRLS;
  END IF;
END
$roles$;
CREATE SCHEMA auth;
CREATE TABLE auth.users (id uuid PRIMARY KEY);
CREATE FUNCTION auth.uid() RETURNS uuid
LANGUAGE sql STABLE SET search_path = '' AS $$
  SELECT nullif(current_setting('request.jwt.claims', true)::json->>'sub', '')::uuid
$$;
GRANT USAGE ON SCHEMA auth TO anon, authenticated;
GRANT EXECUTE ON FUNCTION auth.uid() TO anon, authenticated;
"""


def _dsn(url: str, database: str) -> str:
    return (
        make_url(url)
        .set(drivername="postgresql", database=database)
        .render_as_string(hide_password=False)
    )


@pytest.fixture(scope="session")
def copied_schema_dsn(request: pytest.FixtureRequest):
    local_override = os.getenv("DB05_LOCAL_TEST_DATABASE_URL")
    pg_url = local_override or request.getfixturevalue("_pg_url")
    parsed_url = make_url(pg_url)
    if local_override:
        assert parsed_url.host in {"127.0.0.1", "localhost", "::1"}, (
            "DB05_LOCAL_TEST_DATABASE_URL must target loopback"
        )

    base_database = parsed_url.database
    assert base_database
    admin_dsn = _dsn(pg_url, base_database)
    test_dsn = _dsn(pg_url, _DB_NAME)

    async def setup() -> None:
        admin = await asyncpg.connect(admin_dsn)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{_DB_NAME}"')
            await admin.execute(f'CREATE DATABASE "{_DB_NAME}"')
        finally:
            await admin.close()

        conn = await asyncpg.connect(test_dsn)
        try:
            await conn.execute(_AUTH_BOOTSTRAP)
            await conn.execute(_SNAPSHOT.read_text(encoding="utf-8"))
            await conn.execute(_CREATE.read_text(encoding="utf-8"))
            await conn.execute(_SEED.read_text(encoding="utf-8"))
            await conn.execute(_COPY.read_text(encoding="utf-8"))
            await conn.execute(_RECONCILE.read_text(encoding="utf-8"))
        finally:
            await conn.close()

    async def teardown() -> None:
        admin = await asyncpg.connect(admin_dsn)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{_DB_NAME}" WITH (FORCE)')
        finally:
            await admin.close()

    asyncio.run(setup())
    try:
        yield test_dsn
    finally:
        asyncio.run(teardown())


@pytest_asyncio.fixture
async def copied_conn(copied_schema_dsn: str):
    conn = await asyncpg.connect(copied_schema_dsn)
    try:
        yield conn
    finally:
        await conn.close()


async def _target_fingerprint(conn: asyncpg.Connection) -> tuple[tuple[str, int, str], ...]:
    tables = await conn.fetch(
        """
        SELECT table_schema, table_name
        FROM information_schema.tables
        WHERE table_schema IN ('app','internal') AND table_type='BASE TABLE'
        ORDER BY table_schema,table_name
        """
    )
    result: list[tuple[str, int, str]] = []
    for row in tables:
        qualified = f'"{row["table_schema"]}"."{row["table_name"]}"'
        count, checksum = await conn.fetchrow(
            f"""
            SELECT count(*), COALESCE(
                md5(string_agg(row_hash,'' ORDER BY row_hash)),md5(''))
            FROM (SELECT md5(to_jsonb(t)::text) AS row_hash FROM {qualified} t) hashed
            """
        )
        result.append((qualified, count, checksum))
    return tuple(result)


@pytest.mark.asyncio
async def test_forward_copy_reconcile_idempotency_and_reverse_delta(copied_conn):
    # The scrubbed fixture makes every one of the 42 legacy tables non-empty.
    legacy_tables = await copied_conn.fetch(
        """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema='public' AND table_type='BASE TABLE'
        ORDER BY table_name
        """
    )
    assert len(legacy_tables) == 42
    for row in legacy_tables:
        table_name = row["table_name"].replace('"', '""')
        assert await copied_conn.fetchval(f'SELECT count(*) FROM "{table_name}"') > 0

    # Every target mapping is exercised by that seed.
    table_counts = await _target_fingerprint(copied_conn)
    assert len(table_counts) == 33
    assert all(row_count > 0 for _, row_count, _ in table_counts)

    course = await copied_conn.fetchrow(
        """
        SELECT current_week,retrieval_weights,retrieval_weight_min,retrieval_weight_max
        FROM app.courses WHERE id=1
        """
    )
    assert course["current_week"] == 3
    assert json.loads(course["retrieval_weights"]) == {"semantic": 0.7, "keyword": 0.3}
    assert course["retrieval_weight_min"] == pytest.approx(0.1)
    assert course["retrieval_weight_max"] == pytest.approx(0.9)

    concept = await copied_conn.fetchrow(
        "SELECT canonical_symbols,symbol_metadata FROM app.concepts WHERE id=31"
    )
    assert concept["canonical_symbols"] == ["p", "v"]
    assert json.loads(concept["symbol_metadata"]) == {"convention": "SI"}
    problem = await copied_conn.fetchrow(
        "SELECT problem_text,payload_extra FROM app.problems WHERE id=32"
    )
    assert problem["problem_text"] == "Find the pressure"
    assert json.loads(problem["payload_extra"]) == {"future_key": "kept"}
    assert await copied_conn.fetchval(
        "SELECT count(*) FROM app.question_opportunities"
    ) == 2
    assert await copied_conn.fetchval(
        "SELECT count(*) FROM internal.grading_runs"
    ) == 2

    before = await _target_fingerprint(copied_conn)
    await copied_conn.execute(_COPY.read_text(encoding="utf-8"))
    await copied_conn.execute(_RECONCILE.read_text(encoding="utf-8"))
    assert await _target_fingerprint(copied_conn) == before

    # Simulate target-only writes after cutover, then reverse them under a write pause.
    await copied_conn.execute(
        """
        WITH new_course AS (
          INSERT INTO app.courses
            (name,slug,subject_name,created_at,updated_at)
          VALUES ('Reverse Course','reverse-course','Math','2030-01-01','2030-01-01')
          RETURNING id
        ), new_document AS (
          INSERT INTO app.documents
            (course_id,title,content,content_hash,status,created_at,updated_at)
          SELECT id,'Reverse document','delta','reverse-doc-hash','ready',
                 '2030-01-01','2030-01-01' FROM new_course
          RETURNING id
        )
        INSERT INTO app.chat_sessions
          (external_id,user_id,course_id,metadata,created_at,updated_at)
        SELECT 'reverse-chat','10000000-0000-4000-8000-000000000001',id,
               '{"delta":true}','2030-01-01','2030-01-01' FROM new_course;
        """
    )
    transaction = copied_conn.transaction()
    await transaction.start()
    try:
        await copied_conn.execute(
            "SET LOCAL db05.rollback_watermark = '2029-12-31T00:00:00Z'"
        )
        await copied_conn.execute(_REVERSE.read_text(encoding="utf-8"))
    except BaseException:
        await transaction.rollback()
        raise
    else:
        await transaction.commit()

    assert await copied_conn.fetchval(
        "SELECT count(*) FROM aita_search_spaces WHERE slug='reverse-course'"
    ) == 1
    assert await copied_conn.fetchval(
        "SELECT count(*) FROM aita_documents WHERE content_hash='reverse-doc-hash'"
    ) == 1
    assert await copied_conn.fetchval(
        "SELECT count(*) FROM chat_sessions WHERE chat_id='reverse-chat'"
    ) == 1
    await copied_conn.execute(_RECONCILE.read_text(encoding="utf-8"))


def test_copy_migration_is_non_destructive_and_names_exactly_six_drops():
    sql = _COPY.read_text(encoding="utf-8").lower()
    executable = "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    )
    assert not any(
        token in executable
        for token in ("delete from", "truncate", "drop table", "alter table")
    )
    approved_drops = {
        "apollo_kg_entries",
        "apollo_clarifications",
        "apollo_misconceptions",
        "apollo_misconception_observations",
        "apollo_provisioning_jobs",
        "apollo_rejected_problems",
    }
    comment = sql.split("begin;", 1)[0]
    assert {name for name in approved_drops if f"public.{name}" in comment} == approved_drops
    assert comment.count("public.apollo_") == 6
