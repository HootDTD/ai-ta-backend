"""Local-Docker rehearsal for the DB-16 delayed legacy teardown.

``scripts/db/remove_legacy_public_schema.sql`` is a separately-gated, human-run
script -- never part of ``supabase/migrations/`` and never auto-applied by
``supabase db reset`` (see that file's banner). This suite is the rehearsal
plan section 8.3 asks for: apply the forward chain + copy on a throwaway
database, then prove (a) the teardown's own preconditions pass and every
legacy table disappears in one pass with no CASCADE anywhere, and (b) a
tampered precondition -- a copied row deleted out from under the
reconciliation check, or a no-copy table already missing -- aborts the whole
script before a single DROP runs.

Each test rebuilds its own throwaway database (function-scoped): the teardown
is destructive to the legacy schema, so tests cannot share one mutated
database the way ``test_copy_app_schema_v1.py`` does.
"""

from __future__ import annotations

import asyncio
import os
import re
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
_TEARDOWN = _REPO / "scripts" / "db" / "remove_legacy_public_schema.sql"
_DB_NAME = "remove_legacy_public_schema"

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

# The full 42-table child-first drop order this migration must reproduce
# exactly (see the DB-16 report for the FK-web derivation). Mirrors the
# section headers inside scripts/db/remove_legacy_public_schema.sql.
_EXPECTED_DROP_ORDER = [
    # Section 5: the 7 approved no-copy tables, section 4 order + A6.
    "apollo_clarifications",
    "apollo_misconception_observations",
    "apollo_misconceptions",
    "apollo_rejected_problems",
    "apollo_provisioning_jobs",
    "apollo_kg_entries",
    "apollo_kg_negotiations",
    # Section 6a: leaves.
    "chat_turns",
    "chat_session_snippets",
    "chat_router_decisions",
    "teacher_upload_jobs",
    "course_invite_links",
    "teacher_courses",
    "apollo_messages",
    "apollo_grading_artifacts",
    "apollo_reference_question_opportunities",
    "apollo_question_tally",
    "apollo_authored_sets",
    "apollo_entity_prereqs",
    "apollo_learner_state",
    "apollo_mastery_events",
    "apollo_graph_comparison_findings",
    "apollo_dedup_decisions",
    "apollo_ingest_errors",
    "apollo_ingest_page_evidence",
    "apollo_generation_runs",
    "ai_use_reports",
    "apollo_student_progress",
    # Section 6b.
    "aita_chunks",
    "teacher_uploads",
    "chat_sessions",
    "apollo_kg_entities",
    "apollo_ingest_runs",
    "apollo_graph_comparison_runs",
    "apollo_concept_problems",
    "course_memberships",
    # Section 6c.
    "aita_documents",
    "apollo_problem_attempts",
    # Section 6d.
    "apollo_sessions",
    # Section 6e.
    "apollo_concepts",
    # Section 6f.
    "apollo_subjects",
    # Section 6g: the global FK root, LAST.
    "aita_search_spaces",
]


def _dsn(url: str, database: str) -> str:
    return (
        make_url(url)
        .set(drivername="postgresql", database=database)
        .render_as_string(hide_password=False)
    )


@pytest.fixture
def teardown_schema_dsn(request: pytest.FixtureRequest):
    """A fresh throwaway database with the forward chain + copy already applied.

    Sync, function-scoped (unlike ``test_copy_app_schema_v1.py``'s sync,
    session-scoped ``copied_schema_dsn``): the teardown physically drops the
    legacy schema, so every test needs its own independent copy to mutate.
    Sync (not ``pytest_asyncio.fixture``) for the same reason as its sibling
    -- it calls ``asyncio.run()`` internally and must not execute inside an
    already-running event loop, which `request.getfixturevalue("_pg_url")`
    would trip over from inside an async fixture.
    """
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
            await admin.execute(f'DROP DATABASE IF EXISTS "{_DB_NAME}" WITH (FORCE)')
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
async def teardown_conn(teardown_schema_dsn: str):
    conn = await asyncpg.connect(teardown_schema_dsn)
    try:
        yield conn
    finally:
        await conn.close()


async def _public_table_count(conn: asyncpg.Connection) -> int:
    return await conn.fetchval(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema='public' AND table_type='BASE TABLE'"
    )


@pytest.mark.asyncio
async def test_teardown_succeeds_on_correctly_copied_database(teardown_conn):
    # Sanity: the seeded, freshly-copied database starts with all 42 legacy
    # tables and the full 28-table target populated.
    assert await _public_table_count(teardown_conn) == 42
    assert (
        await teardown_conn.fetchval(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema IN ('app','internal') AND table_type='BASE TABLE'"
        )
        == 28
    )
    courses_before = await teardown_conn.fetchval("SELECT count(*) FROM app.courses")
    assert courses_before > 0

    # The teardown script is its own transaction (BEGIN...COMMIT); a clean run
    # must not raise.
    await teardown_conn.execute(_TEARDOWN.read_text(encoding="utf-8"))

    # Every legacy public table is gone -- checked by exact name (matching the
    # migration's own post-check), not a blanket "public is empty" assumption.
    assert await _public_table_count(teardown_conn) == 0
    for table_name in _EXPECTED_DROP_ORDER:
        exists = await teardown_conn.fetchval(
            "SELECT to_regclass('public.' || $1) IS NOT NULL", table_name
        )
        assert not exists, f"public.{table_name} still exists after teardown"

    # The target schemas are untouched: same table count, same course data.
    assert (
        await teardown_conn.fetchval(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema IN ('app','internal') AND table_type='BASE TABLE'"
        )
        == 28
    )
    assert await teardown_conn.fetchval("SELECT count(*) FROM app.courses") == courses_before


@pytest.mark.asyncio
async def test_teardown_aborts_when_a_copied_row_is_missing(teardown_conn):
    # Tamper with the copy AFTER it ran: delete one of the two seeded
    # course_memberships rows from the target, so source (2 rows) and target
    # (1 row) no longer match -- the 'course_memberships' reconciliation
    # mapping must catch this and abort before any DROP runs.
    deleted = await teardown_conn.fetchval(
        "DELETE FROM app.course_memberships "
        "WHERE ctid = (SELECT ctid FROM app.course_memberships LIMIT 1) "
        "RETURNING user_id"
    )
    assert deleted is not None

    with pytest.raises(asyncpg.PostgresError, match="copy reconciliation failed"):
        await teardown_conn.execute(_TEARDOWN.read_text(encoding="utf-8"))
    # The failed script's BEGIN was never COMMITted; Postgres leaves the
    # connection in an aborted-transaction state until an explicit ROLLBACK.
    await teardown_conn.execute("ROLLBACK")

    # The teardown's own BEGIN/COMMIT rolled back: nothing was dropped.
    assert await _public_table_count(teardown_conn) == 42


@pytest.mark.asyncio
async def test_teardown_aborts_when_a_no_copy_table_is_missing(teardown_conn):
    # Simulate drift: an operator (or a previous partial run) already removed
    # an approved no-copy table out of band. The precondition must catch the
    # gap and abort loudly rather than silently skip it. A throwaway filler
    # table keeps the total public count at 42 so this exercises section 1's
    # specific per-table check, not section 0's broader count precheck (which
    # would otherwise fire first and mask it).
    await teardown_conn.execute("DROP TABLE public.apollo_kg_negotiations")
    await teardown_conn.execute("CREATE TABLE public._test_filler (id int)")
    assert await _public_table_count(teardown_conn) == 42

    with pytest.raises(asyncpg.PostgresError, match="apollo_kg_negotiations.*is missing"):
        await teardown_conn.execute(_TEARDOWN.read_text(encoding="utf-8"))
    await teardown_conn.execute("ROLLBACK")

    # Only the pre-existing, out-of-band changes are reflected -- the
    # teardown script itself dropped nothing before aborting.
    assert await _public_table_count(teardown_conn) == 42
    assert await teardown_conn.fetchval(
        "SELECT to_regclass('public.apollo_kg_negotiations') IS NULL"
    )


def test_teardown_script_drop_order_is_full_child_first_and_never_cascades():
    """Static regression guard: no Testcontainers/Docker required.

    Pins the exact 42-table child-first order derived from the FK web in
    .planning/cleanup/findings/c-db-usage.md, independent of the DB harness,
    so an accidental reordering fails fast without Docker.
    """
    sql = _TEARDOWN.read_text(encoding="utf-8")
    executable = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))

    assert "CASCADE" not in executable.upper()

    drop_statements = re.findall(r"DROP TABLE public\.(\w+);", executable)
    assert drop_statements == _EXPECTED_DROP_ORDER
    assert len(drop_statements) == 42
    assert len(set(drop_statements)) == 42, "a table name is dropped more than once"

    # The 7 no-copy tables must precede all 35 others (section 4/A6 ordering).
    no_copy = _EXPECTED_DROP_ORDER[:7]
    assert set(no_copy) == {
        "apollo_clarifications",
        "apollo_misconception_observations",
        "apollo_misconceptions",
        "apollo_rejected_problems",
        "apollo_provisioning_jobs",
        "apollo_kg_entries",
        "apollo_kg_negotiations",
    }

    # aita_search_spaces (the 21-inbound-reference FK root) drops last.
    assert drop_statements[-1] == "aita_search_spaces"

    # Every FK-safe ordering constraint the report cites explicitly: child
    # before parent for each of the FK web's named cases.
    index = {name: i for i, name in enumerate(drop_statements)}
    parent_child_pairs = [
        ("apollo_graph_comparison_findings", "apollo_graph_comparison_runs"),
        ("apollo_mastery_events", "apollo_kg_entities"),
        ("apollo_entity_prereqs", "apollo_kg_entities"),
        ("apollo_messages", "apollo_sessions"),
        ("apollo_ingest_page_evidence", "apollo_ingest_runs"),
        ("apollo_ingest_errors", "apollo_ingest_runs"),
        ("apollo_problem_attempts", "apollo_sessions"),
        ("aita_chunks", "aita_documents"),
        ("teacher_uploads", "aita_documents"),
        ("teacher_upload_jobs", "teacher_uploads"),
        ("apollo_concept_problems", "apollo_concepts"),
        ("apollo_concepts", "apollo_subjects"),
        ("apollo_sessions", "apollo_concepts"),
        ("apollo_subjects", "aita_search_spaces"),
        # RLS-policy dependencies (migrations 006/008/015): a table whose
        # policy references another table must drop before it, same as an FK
        # child -- plain DROP TABLE enforces this even with no FK involved.
        ("chat_turns", "chat_sessions"),
        ("chat_session_snippets", "chat_sessions"),
        ("chat_router_decisions", "chat_sessions"),
        ("teacher_courses", "course_memberships"),
        ("teacher_uploads", "course_memberships"),
        ("course_invite_links", "course_memberships"),
    ]
    for child, parent in parent_child_pairs:
        assert index[child] < index[parent], f"{child} must drop before {parent}"
