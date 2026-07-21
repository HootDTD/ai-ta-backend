"""Real-Postgres contract tests for the DB-04 app schema v1 migration.

The session pgvector Testcontainers fixture supplies a local Docker server. This
module creates a fresh database on that server, installs the minimal Supabase
Auth role/function shim, applies the DB-03 snapshot, and then applies DB-04.
Nothing here connects to a remote Supabase project.
"""

from __future__ import annotations

import asyncio
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
_MIGRATION = _MIGRATIONS / "20260717035041_create_app_schema_v1.sql"
_DB_NAME = "app_schema_v1"

STUDENT = "10000000-0000-4000-8000-000000000001"
TEACHER = "20000000-0000-4000-8000-000000000002"
OTHER_STUDENT = "30000000-0000-4000-8000-000000000003"
NO_MEMBERSHIP = "40000000-0000-4000-8000-000000000004"

APP_TABLES = {
    "ai_usage_reports",
    "chat_messages",
    "concepts",
    "course_invites",
    "course_memberships",
    "courses",
    "documents",
    "learning_activities",
    "learner_entities",
    "learner_state",
    "mastery_events",
    "problem_attempts",
    "problems",
    "provisioning_runs",
    "question_opportunities",
    "student_progress",
    "tutoring_messages",
    "uploads",
}

INTERNAL_TABLES = {
    "chat_routing_decisions",
    "chat_session_snippets",
    "content_ingest_errors",
    "content_ingest_runs",
    "dedup_decisions",
    "document_chunks",
    "entity_prerequisites",
    "grading_runs",
    "ingest_page_evidence",
    "upload_jobs",
}

EXPECTED_PKS = {
    "app.ai_usage_reports": ("id",),
    "app.chat_messages": ("id",),
    "app.concepts": ("id",),
    "app.course_invites": ("id",),
    "app.course_memberships": ("user_id", "course_id"),
    "app.courses": ("id",),
    "app.documents": ("id",),
    "app.learning_activities": ("id",),
    "app.learner_entities": ("id",),
    "app.learner_state": ("user_id", "course_id", "entity_id"),
    "app.mastery_events": ("id",),
    "app.problem_attempts": ("id",),
    "app.problems": ("id",),
    "app.provisioning_runs": ("id",),
    "app.question_opportunities": ("id",),
    "app.student_progress": ("user_id", "course_id"),
    "app.tutoring_messages": ("id",),
    "app.uploads": ("id",),
    "internal.chat_routing_decisions": ("id",),
    "internal.chat_session_snippets": ("learning_activity_id", "chunk_id"),
    "internal.content_ingest_errors": ("id",),
    "internal.content_ingest_runs": ("id",),
    "internal.dedup_decisions": ("id",),
    "internal.document_chunks": ("id",),
    "internal.entity_prerequisites": ("from_entity_id", "to_entity_id"),
    "internal.grading_runs": ("id",),
    "internal.ingest_page_evidence": ("id",),
    "internal.upload_jobs": ("id",),
}

# local table -> local columns -> referenced table and columns
EXPECTED_FKS = {
    "app.ai_usage_reports": {
        ("user_id",): ("auth.users", ("id",)),
        ("course_id",): ("app.courses", ("id",)),
        ("chat_id",): ("app.learning_activities", ("external_id",)),
    },
    "app.provisioning_runs": {
        ("course_id",): ("app.courses", ("id",)),
        ("problem_document_id",): ("app.documents", ("id",)),
        ("solution_document_id",): ("app.documents", ("id",)),
        ("concept_id",): ("app.concepts", ("id",)),
        ("ingest_run_id",): ("internal.content_ingest_runs", ("id",)),
    },
    "app.chat_messages": {
        ("course_id",): ("app.courses", ("id",)),
        ("learning_activity_id",): ("app.learning_activities", ("id",)),
    },
    "app.learning_activities": {
        ("user_id",): ("auth.users", ("id",)),
        ("course_id",): ("app.courses", ("id",)),
        ("concept_id",): ("app.concepts", ("id",)),
        ("current_problem_id",): ("app.problems", ("id",)),
    },
    "app.concepts": {
        ("course_id",): ("app.courses", ("id",)),
    },
    "app.course_invites": {
        ("course_id",): ("app.courses", ("id",)),
        ("created_by",): ("auth.users", ("id",)),
    },
    "app.course_memberships": {
        ("user_id",): ("auth.users", ("id",)),
        ("course_id",): ("app.courses", ("id",)),
    },
    "app.documents": {("course_id",): ("app.courses", ("id",))},
    "app.learner_entities": {
        ("course_id",): ("app.courses", ("id",)),
        ("concept_id",): ("app.concepts", ("id",)),
    },
    "app.learner_state": {
        ("user_id",): ("auth.users", ("id",)),
        ("course_id",): ("app.courses", ("id",)),
        ("entity_id",): ("app.learner_entities", ("id",)),
    },
    "app.mastery_events": {
        ("user_id",): ("auth.users", ("id",)),
        ("course_id",): ("app.courses", ("id",)),
        ("entity_id",): ("app.learner_entities", ("id",)),
        ("attempt_id",): ("app.problem_attempts", ("id",)),
        ("concept_problem_id",): ("app.problems", ("id",)),
    },
    "app.problem_attempts": {
        ("course_id",): ("app.courses", ("id",)),
        ("user_id",): ("auth.users", ("id",)),
        ("learning_activity_id",): ("app.learning_activities", ("id",)),
        ("problem_id",): ("app.problems", ("id",)),
    },
    "app.problems": {
        ("course_id",): ("app.courses", ("id",)),
        ("concept_id",): ("app.concepts", ("id",)),
    },
    "app.question_opportunities": {
        ("course_id",): ("app.courses", ("id",)),
        ("learning_activity_id",): ("app.learning_activities", ("id",)),
        ("attempt_id",): ("app.problem_attempts", ("id",)),
    },
    "app.student_progress": {
        ("user_id",): ("auth.users", ("id",)),
        ("course_id",): ("app.courses", ("id",)),
    },
    "app.tutoring_messages": {
        ("course_id",): ("app.courses", ("id",)),
        ("learning_activity_id",): ("app.learning_activities", ("id",)),
        ("attempt_id",): ("app.problem_attempts", ("id",)),
    },
    "app.uploads": {
        ("course_id",): ("app.courses", ("id",)),
        ("document_id",): ("app.documents", ("id",)),
        ("uploaded_by",): ("auth.users", ("id",)),
    },
    "internal.chat_routing_decisions": {
        ("course_id",): ("app.courses", ("id",)),
        ("learning_activity_id",): ("app.learning_activities", ("id",)),
    },
    "internal.chat_session_snippets": {
        ("learning_activity_id",): ("app.learning_activities", ("id",)),
        ("chunk_id",): ("internal.document_chunks", ("id",)),
        ("course_id",): ("app.courses", ("id",)),
    },
    "internal.content_ingest_errors": {
        ("course_id",): ("app.courses", ("id",)),
        ("ingest_run_id",): ("internal.content_ingest_runs", ("id",)),
    },
    "internal.content_ingest_runs": {
        ("course_id",): ("app.courses", ("id",)),
        ("document_id",): ("app.documents", ("id",)),
    },
    "internal.dedup_decisions": {
        ("course_id",): ("app.courses", ("id",)),
        ("ingest_run_id",): ("internal.content_ingest_runs", ("id",)),
        ("concept_id",): ("app.concepts", ("id",)),
        ("matched_entity_id",): ("app.learner_entities", ("id",)),
    },
    "internal.document_chunks": {
        ("course_id",): ("app.courses", ("id",)),
        ("document_id",): ("app.documents", ("id",)),
    },
    "internal.entity_prerequisites": {
        ("course_id",): ("app.courses", ("id",)),
        ("from_entity_id",): ("app.learner_entities", ("id",)),
        ("to_entity_id",): ("app.learner_entities", ("id",)),
    },
    "internal.grading_runs": {
        ("course_id",): ("app.courses", ("id",)),
        ("user_id",): ("auth.users", ("id",)),
        ("attempt_id",): ("app.problem_attempts", ("id",)),
        ("concept_id",): ("app.concepts", ("id",)),
        ("problem_id",): ("app.problems", ("id",)),
    },
    "internal.ingest_page_evidence": {
        ("course_id",): ("app.courses", ("id",)),
        ("ingest_run_id",): ("internal.content_ingest_runs", ("id",)),
        ("document_id",): ("app.documents", ("id",)),
    },
    "internal.upload_jobs": {
        ("course_id",): ("app.courses", ("id",)),
        ("upload_id",): ("app.uploads", ("id",)),
    },
}

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
def app_schema_v1_dsn(request: pytest.FixtureRequest):
    local_override = os.getenv("DB04_LOCAL_TEST_DATABASE_URL")
    pg_url = local_override or request.getfixturevalue("_pg_url")
    parsed_url = make_url(pg_url)
    if local_override:
        assert parsed_url.host in {"127.0.0.1", "localhost", "::1"}, (
            "DB04_LOCAL_TEST_DATABASE_URL must target loopback"
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
            await conn.execute(_MIGRATION.read_text(encoding="utf-8"))
            await conn.executemany(
                "INSERT INTO auth.users (id) VALUES ($1)",
                [(STUDENT,), (TEACHER,), (OTHER_STUDENT,), (NO_MEMBERSHIP,)],
            )
            course_1 = await conn.fetchval(
                "INSERT INTO app.courses (name, slug, subject_name) "
                "VALUES ('Course One', 'course-one', 'Physics') RETURNING id"
            )
            course_2 = await conn.fetchval(
                "INSERT INTO app.courses (name, slug, subject_name) "
                "VALUES ('Course Two', 'course-two', 'Math') RETURNING id"
            )
            await conn.executemany(
                "INSERT INTO app.course_memberships (user_id, course_id, role) VALUES ($1, $2, $3)",
                [
                    (STUDENT, course_1, "student"),
                    (TEACHER, course_1, "teacher"),
                    (OTHER_STUDENT, course_2, "student"),
                ],
            )
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
async def schema_conn(app_schema_v1_dsn: str):
    conn = await asyncpg.connect(app_schema_v1_dsn)
    try:
        yield conn
    finally:
        await conn.close()


async def _set_authenticated(conn: asyncpg.Connection, user_id: str) -> None:
    await conn.execute("SET LOCAL ROLE authenticated")
    await conn.execute(
        "SELECT set_config('request.jwt.claims', $1, true)",
        f'{{"sub":"{user_id}"}}',
    )


@pytest.mark.asyncio
async def test_all_28_tables_have_expected_primary_keys_and_foreign_keys(schema_conn):
    rows = await schema_conn.fetch(
        "SELECT schemaname, tablename FROM pg_tables WHERE schemaname IN ('app', 'internal')"
    )
    actual_tables = {f"{row['schemaname']}.{row['tablename']}" for row in rows}
    assert actual_tables == set(EXPECTED_PKS)

    pk_rows = await schema_conn.fetch(
        """
        SELECT n.nspname AS schema_name, c.relname AS table_name,
               array_agg(a.attname ORDER BY key_columns.ordinality) AS columns
        FROM pg_constraint AS constraint_row
        JOIN pg_class AS c ON c.oid = constraint_row.conrelid
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        JOIN LATERAL unnest(constraint_row.conkey) WITH ORDINALITY
          AS key_columns(attnum, ordinality) ON true
        JOIN pg_attribute AS a
          ON a.attrelid = c.oid AND a.attnum = key_columns.attnum
        WHERE constraint_row.contype = 'p' AND n.nspname IN ('app', 'internal')
        GROUP BY n.nspname, c.relname
        """
    )
    actual_pks = {
        f"{row['schema_name']}.{row['table_name']}": tuple(row["columns"]) for row in pk_rows
    }
    assert actual_pks == EXPECTED_PKS

    fk_rows = await schema_conn.fetch(
        """
        SELECT source_ns.nspname AS source_schema, source.relname AS source_table,
               array_agg(source_col.attname ORDER BY keys.ordinality) AS source_columns,
               target_ns.nspname AS target_schema, target.relname AS target_table,
               array_agg(target_col.attname ORDER BY keys.ordinality) AS target_columns
        FROM pg_constraint AS constraint_row
        JOIN pg_class AS source ON source.oid = constraint_row.conrelid
        JOIN pg_namespace AS source_ns ON source_ns.oid = source.relnamespace
        JOIN pg_class AS target ON target.oid = constraint_row.confrelid
        JOIN pg_namespace AS target_ns ON target_ns.oid = target.relnamespace
        JOIN LATERAL unnest(constraint_row.conkey, constraint_row.confkey)
          WITH ORDINALITY AS keys(source_attnum, target_attnum, ordinality) ON true
        JOIN pg_attribute AS source_col
          ON source_col.attrelid = source.oid
         AND source_col.attnum = keys.source_attnum
        JOIN pg_attribute AS target_col
          ON target_col.attrelid = target.oid
         AND target_col.attnum = keys.target_attnum
        WHERE constraint_row.contype = 'f'
          AND source_ns.nspname IN ('app', 'internal')
        GROUP BY source_ns.nspname, source.relname,
                 target_ns.nspname, target.relname, constraint_row.oid
        """
    )
    actual_fks: dict[str, dict[tuple[str, ...], tuple[str, tuple[str, ...]]]] = {}
    for row in fk_rows:
        table = f"{row['source_schema']}.{row['source_table']}"
        actual_fks.setdefault(table, {})[tuple(row["source_columns"])] = (
            f"{row['target_schema']}.{row['target_table']}",
            tuple(row["target_columns"]),
        )
    assert actual_fks == EXPECTED_FKS


@pytest.mark.asyncio
async def test_every_app_table_has_forced_rls_and_a_policy(schema_conn):
    rows = await schema_conn.fetch(
        """
        SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
               count(policy_row.policyname) AS policy_count
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        LEFT JOIN pg_policies AS policy_row
          ON policy_row.schemaname = n.nspname AND policy_row.tablename = c.relname
        WHERE n.nspname = 'app' AND c.relkind = 'r'
        GROUP BY c.relname, c.relrowsecurity, c.relforcerowsecurity
        """
    )
    assert {row["relname"] for row in rows} == APP_TABLES
    assert all(row["relrowsecurity"] for row in rows)
    assert all(row["relforcerowsecurity"] for row in rows)
    assert all(row["policy_count"] >= 1 for row in rows)

    policy_count = await schema_conn.fetchval(
        "SELECT count(*) FROM pg_policies WHERE schemaname = 'app'"
    )
    assert policy_count == 32


@pytest.mark.asyncio
async def test_anon_denied_member_read_teacher_write_and_cross_course_denied(schema_conn):
    transaction = schema_conn.transaction()
    await transaction.start()
    try:
        await schema_conn.execute("SET LOCAL ROLE anon")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await schema_conn.fetchval("SELECT count(*) FROM app.courses")
    finally:
        await transaction.rollback()

    transaction = schema_conn.transaction()
    await transaction.start()
    try:
        await _set_authenticated(schema_conn, STUDENT)
        assert await schema_conn.fetchval("SELECT count(*) FROM app.courses") == 1
        assert (
            await schema_conn.fetchval(
                "UPDATE app.courses SET name = 'Student edit' "
                "WHERE slug = 'course-one' RETURNING id"
            )
            is None
        )
    finally:
        await transaction.rollback()

    transaction = schema_conn.transaction()
    await transaction.start()
    try:
        await _set_authenticated(schema_conn, TEACHER)
        assert (
            await schema_conn.fetchval(
                "UPDATE app.courses SET name = 'Teacher edit' "
                "WHERE slug = 'course-one' RETURNING id"
            )
            is not None
        )
        assert (
            await schema_conn.fetchval(
                "UPDATE app.courses SET name = 'Cross-course edit' "
                "WHERE slug = 'course-two' RETURNING id"
            )
            is None
        )
        assert await schema_conn.fetchval("SELECT count(*) FROM app.courses") == 1
    finally:
        await transaction.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_id", "roles", "expected"),
    [
        (STUDENT, ["student"], True),
        (STUDENT, ["teacher"], False),
        (TEACHER, ["teacher"], True),
        (NO_MEMBERSHIP, ["student", "teacher"], False),
    ],
)
async def test_has_course_role_member_teacher_and_none(schema_conn, user_id, roles, expected):
    transaction = schema_conn.transaction()
    await transaction.start()
    try:
        await _set_authenticated(schema_conn, user_id)
        course_id = await schema_conn.fetchval(
            "SELECT id FROM app.courses WHERE slug = 'course-one'"
        )
        # A user with no membership cannot see the course row, so use the fixed
        # identity value seeded first in this fresh database.
        course_id = course_id or 1
        actual = await schema_conn.fetchval(
            "SELECT internal.has_course_role($1, $2::text[])", course_id, roles
        )
        assert actual is expected
    finally:
        await transaction.rollback()


@pytest.mark.asyncio
async def test_internal_schema_is_service_only(schema_conn):
    rows = await schema_conn.fetch(
        """
        SELECT c.relname, c.relrowsecurity
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = 'internal' AND c.relkind = 'r'
        """
    )
    assert {row["relname"] for row in rows} == INTERNAL_TABLES
    assert all(not row["relrowsecurity"] for row in rows)

    transaction = schema_conn.transaction()
    await transaction.start()
    try:
        await _set_authenticated(schema_conn, STUDENT)
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await schema_conn.fetchval("SELECT count(*) FROM internal.document_chunks")
    finally:
        await transaction.rollback()


@pytest.mark.asyncio
async def test_learning_activity_modality_status_and_active_tutoring_semantics(schema_conn):
    course_id = await schema_conn.fetchval(
        "SELECT id FROM app.courses WHERE slug = 'course-one'"
    )
    chat_id = await schema_conn.fetchval(
        """
        INSERT INTO app.learning_activities
          (modality,external_id,user_id,course_id,status)
        VALUES ('chat','schema-v1-chat',$1,$2,'active')
        RETURNING id
        """,
        STUDENT,
        course_id,
    )
    assert chat_id is not None

    with pytest.raises(asyncpg.CheckViolationError):
        await schema_conn.execute(
            """
            INSERT INTO app.learning_activities
              (modality,external_id,user_id,course_id,status)
            VALUES ('chat','invalid-paused-chat',$1,$2,'paused')
            """,
            STUDENT,
            course_id,
        )

    first_tutoring_id = await schema_conn.fetchval(
        """
        INSERT INTO app.learning_activities
          (modality,user_id,course_id,status,phase)
        VALUES ('tutoring',$1,$2,'active','INIT')
        RETURNING id
        """,
        STUDENT,
        course_id,
    )
    assert first_tutoring_id is not None
    with pytest.raises(asyncpg.UniqueViolationError):
        await schema_conn.execute(
            """
            INSERT INTO app.learning_activities
              (modality,user_id,course_id,status,phase)
            VALUES ('tutoring',$1,$2,'active','TEACHING')
            """,
            STUDENT,
            course_id,
        )
    assert await schema_conn.fetchval(
        """
        INSERT INTO app.learning_activities
          (modality,user_id,course_id,status,phase)
        VALUES ('tutoring',$1,$2,'paused','TEACHING')
        RETURNING id
        """,
        STUDENT,
        course_id,
    ) is not None


@pytest.mark.asyncio
async def test_ai_usage_reports_check_constraints(schema_conn):
    course_id = await schema_conn.fetchval(
        "SELECT id FROM app.courses WHERE slug = 'course-one'"
    )
    chat_id = await schema_conn.fetchval(
        """
        INSERT INTO app.learning_activities
          (modality,external_id,user_id,course_id,status)
        VALUES ('chat','ai-usage-reports-check-chat',$1,$2,'active')
        RETURNING external_id
        """,
        STUDENT,
        course_id,
    )

    ok_id = await schema_conn.fetchval(
        """
        INSERT INTO app.ai_usage_reports (id, user_id, course_id, chat_id, jsonld, tool_calls)
        VALUES (gen_random_uuid(), $1, $2, $3, '{"a":1}'::jsonb, '[]'::jsonb)
        RETURNING id
        """,
        STUDENT,
        course_id,
        chat_id,
    )
    assert ok_id is not None

    with pytest.raises(asyncpg.CheckViolationError):
        await schema_conn.execute(
            """
            INSERT INTO app.ai_usage_reports (id, user_id, course_id, chat_id, jsonld)
            VALUES (gen_random_uuid(), $1, $2, $3, '[1,2]'::jsonb)
            """,
            STUDENT,
            course_id,
            chat_id,
        )

    with pytest.raises(asyncpg.CheckViolationError):
        await schema_conn.execute(
            """
            INSERT INTO app.ai_usage_reports (id, user_id, course_id, chat_id, tool_calls)
            VALUES (gen_random_uuid(), $1, $2, $3, '{"not":"array"}'::jsonb)
            """,
            STUDENT,
            course_id,
            chat_id,
        )


@pytest.mark.asyncio
async def test_ai_usage_reports_owner_select_and_insert_policies(schema_conn):
    course_id = await schema_conn.fetchval(
        "SELECT id FROM app.courses WHERE slug = 'course-one'"
    )
    chat_id = await schema_conn.fetchval(
        """
        INSERT INTO app.learning_activities
          (modality,external_id,user_id,course_id,status)
        VALUES ('chat','ai-usage-reports-rls-chat',$1,$2,'active')
        RETURNING external_id
        """,
        STUDENT,
        course_id,
    )

    transaction = schema_conn.transaction()
    await transaction.start()
    try:
        await _set_authenticated(schema_conn, STUDENT)
        report_id = await schema_conn.fetchval(
            """
            INSERT INTO app.ai_usage_reports (id, user_id, course_id, chat_id)
            VALUES (gen_random_uuid(), $1, $2, $3)
            RETURNING id
            """,
            STUDENT,
            course_id,
            chat_id,
        )
        assert report_id is not None
        assert (
            await schema_conn.fetchval(
                "SELECT count(*) FROM app.ai_usage_reports WHERE id = $1", report_id
            )
            == 1
        )
    finally:
        await transaction.rollback()

    transaction = schema_conn.transaction()
    await transaction.start()
    try:
        await _set_authenticated(schema_conn, STUDENT)
        # A student cannot attribute a report to a different user -- WITH CHECK fails.
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await schema_conn.execute(
                """
                INSERT INTO app.ai_usage_reports (id, user_id, course_id, chat_id)
                VALUES (gen_random_uuid(), $1, $2, $3)
                """,
                OTHER_STUDENT,
                course_id,
                chat_id,
            )
    finally:
        await transaction.rollback()

    transaction = schema_conn.transaction()
    await transaction.start()
    try:
        await _set_authenticated(schema_conn, STUDENT)
        own_report_id = await schema_conn.fetchval(
            """
            INSERT INTO app.ai_usage_reports (id, user_id, course_id, chat_id)
            VALUES (gen_random_uuid(), $1, $2, $3)
            RETURNING id
            """,
            STUDENT,
            course_id,
            chat_id,
        )

        # Teachers do NOT receive report contents by default: no SELECT policy
        # grants them read, even though they hold a real course role.
        await _set_authenticated(schema_conn, TEACHER)
        assert (
            await schema_conn.fetchval(
                "SELECT count(*) FROM app.ai_usage_reports WHERE id = $1", own_report_id
            )
            == 0
        )
    finally:
        await transaction.rollback()


def test_migration_never_mutates_a_legacy_public_table():
    sql = _MIGRATION.read_text(encoding="utf-8").lower()
    legacy_prefixes = (
        "aita_",
        "teacher_",
        "chat_turns",
        "chat_sessions",
        "course_",
        "apollo_",
        "ai_use_reports",
    )
    mutating_starts = ("alter table", "drop table", "truncate", "update", "delete from")
    for statement in sql.split(";"):
        compact = " ".join(statement.split())
        if compact.startswith(mutating_starts):
            assert not any(f" {prefix}" in compact for prefix in legacy_prefixes)

    assert "create index ix_" not in sql
