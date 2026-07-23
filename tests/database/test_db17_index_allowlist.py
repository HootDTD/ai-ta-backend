"""DB-17: real-Postgres allowlist assertion pinning the target index inventory.

Plan section 6.2 names its target allowlist against the pre-MIG-AMEND
33-table shape (``chat_sessions``, ``tutoring_sessions``, ``authored_sets``,
...). MIG-AMEND (2026-07-20, rulings A5-A9) restructured the built DDL to 28
tables before this test was written -- ``create_app_schema_v1.sql`` is now
the shape authority (see ``docs/architecture/domain-data.md``'s "MIG-AMEND"
paragraph and the DB-17 report for the full reconciliation). This module
therefore pins the actual, applied DDL's index inventory verbatim (extracted
by running the real migration against Testcontainers Postgres and
introspecting ``pg_index``/``pg_am`` -- not hand-transcribed from the plan
text) rather than re-deriving section 6.2's literal per-table bullet list,
and separately verifies the two invariants section 6.2 cares about: no
duplicate indexes, and FK columns covered by a leftmost-prefix index.

FK-coverage finding (see the DB-17 report for the full list): 15 of the 75
foreign keys in the built DDL are NOT covered by a leftmost-prefix index.
Every one follows the same pattern -- the table's composite index puts
``course_id`` (the RLS tenant-scope column) leftmost, leaving the FK's own
column as a non-leading member instead of getting a dedicated or
reversed-order index (the DDL applies that reversal only for
``mastery_events``/``grading_runs``, which each carry a genuine
both-directions read pattern). Plan section 13's acceptance criteria gates
advisor output only on ``mutable-search-path``/``open-anon``/``initplan``
lints, not ``unindexed_foreign_keys`` -- so this is flagged here as a pinned,
named, known-gap set (``KNOWN_UNCOVERED_FKS``) for a future index-hygiene
pass, not silently fixed by this task. Any FK added later without a
conscious coverage decision -- covered or explicitly added to the known-gap
set -- fails this test loudly.

Follows ``test_app_schema_v1.py``'s harness conventions (fresh throwaway
database per session, ``_AUTH_BOOTSTRAP`` shim, real migration files applied
verbatim).
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
_DB_NAME = "db17_index_allowlist"

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

# Complete app/internal index inventory of the applied `create_app_schema_v1`
# DDL, captured by running the migration against real Postgres and
# introspecting pg_index/pg_am (see the DB-17 report for the capture method).
# Each row: (schema.table, index_name, access_method, is_unique, is_primary,
# ordered key columns/expressions, partial predicate or None).
EXPECTED_INDEXES: set[tuple[str, str, str, bool, bool, tuple[str, ...], str | None]] = {
    (
        "app.ai_usage_reports",
        "ai_usage_reports__chat_id__idx",
        "btree",
        False,
        False,
        ("chat_id",),
        None,
    ),
    (
        "app.ai_usage_reports",
        "ai_usage_reports__user_course__idx",
        "btree",
        False,
        False,
        ("user_id", "course_id"),
        None,
    ),
    ("app.ai_usage_reports", "ai_usage_reports_pkey", "btree", True, True, ("id",), None),
    (
        "app.chat_messages",
        "chat_messages__course_activity__idx",
        "btree",
        False,
        False,
        ("course_id", "learning_activity_id"),
        None,
    ),
    (
        "app.chat_messages",
        "chat_messages_learning_activity_id_external_id_key",
        "btree",
        True,
        False,
        ("learning_activity_id", "external_id"),
        None,
    ),
    (
        "app.chat_messages",
        "chat_messages_learning_activity_id_turn_index_key",
        "btree",
        True,
        False,
        ("learning_activity_id", "turn_index"),
        None,
    ),
    ("app.chat_messages", "chat_messages_pkey", "btree", True, True, ("id",), None),
    (
        "app.concepts",
        "concepts__course_subject__idx",
        "btree",
        False,
        False,
        ("course_id", "subject_slug"),
        None,
    ),
    (
        "app.concepts",
        "concepts_course_id_subject_slug_slug_key",
        "btree",
        True,
        False,
        ("course_id", "subject_slug", "slug"),
        None,
    ),
    ("app.concepts", "concepts_pkey", "btree", True, True, ("id",), None),
    (
        "app.course_invites",
        "course_invites__active_code__uidx",
        "btree",
        True,
        False,
        ("code",),
        "is_active",
    ),
    (
        "app.course_invites",
        "course_invites__created_by_course__idx",
        "btree",
        False,
        False,
        ("created_by", "course_id"),
        None,
    ),
    ("app.course_invites", "course_invites_pkey", "btree", True, True, ("id",), None),
    (
        "app.course_memberships",
        "course_memberships__course_role_user__idx",
        "btree",
        False,
        False,
        ("course_id", "role", "user_id"),
        None,
    ),
    (
        "app.course_memberships",
        "course_memberships_pkey",
        "btree",
        True,
        True,
        ("user_id", "course_id"),
        None,
    ),
    ("app.courses", "courses_pkey", "btree", True, True, ("id",), None),
    ("app.courses", "courses_slug_key", "btree", True, False, ("slug",), None),
    (
        "app.documents",
        "documents__course_created__idx",
        "btree",
        False,
        False,
        ("course_id", "created_at"),
        None,
    ),
    ("app.documents", "documents_content_hash_key", "btree", True, False, ("content_hash",), None),
    ("app.documents", "documents_pkey", "btree", True, True, ("id",), None),
    (
        "app.documents",
        "documents_unique_identifier_hash_key",
        "btree",
        True,
        False,
        ("unique_identifier_hash",),
        None,
    ),
    (
        "app.learner_entities",
        "learner_entities__course_concept__idx",
        "btree",
        False,
        False,
        ("course_id", "concept_id"),
        None,
    ),
    (
        "app.learner_entities",
        "learner_entities_concept_id_canonical_key_key",
        "btree",
        True,
        False,
        ("concept_id", "canonical_key"),
        None,
    ),
    ("app.learner_entities", "learner_entities_pkey", "btree", True, True, ("id",), None),
    (
        "app.learner_state",
        "learner_state__entity_id__idx",
        "btree",
        False,
        False,
        ("entity_id",),
        None,
    ),
    (
        "app.learner_state",
        "learner_state_pkey",
        "btree",
        True,
        True,
        ("user_id", "course_id", "entity_id"),
        None,
    ),
    (
        "app.learning_activities",
        "learning_activities__active_tutoring_user_course__uidx",
        "btree",
        True,
        False,
        ("user_id", "course_id"),
        "((status = 'active'::text) AND (modality = 'tutoring'::text))",
    ),
    (
        "app.learning_activities",
        "learning_activities__current_problem_id__idx",
        "btree",
        False,
        False,
        ("current_problem_id",),
        None,
    ),
    (
        "app.learning_activities",
        "learning_activities__user_course__idx",
        "btree",
        False,
        False,
        ("user_id", "course_id"),
        None,
    ),
    (
        "app.learning_activities",
        "learning_activities_external_id_key",
        "btree",
        True,
        False,
        ("external_id",),
        None,
    ),
    ("app.learning_activities", "learning_activities_pkey", "btree", True, True, ("id",), None),
    (
        "app.mastery_events",
        "mastery_events__attempt_entity_kind__uniq",
        "btree",
        True,
        False,
        ("attempt_id", "entity_id", "event_kind"),
        None,
    ),
    (
        "app.mastery_events",
        "mastery_events__attempt_id__idx",
        "btree",
        False,
        False,
        ("attempt_id",),
        None,
    ),
    (
        "app.mastery_events",
        "mastery_events__concept_problem_id__idx",
        "btree",
        False,
        False,
        ("concept_problem_id",),
        None,
    ),
    (
        "app.mastery_events",
        "mastery_events__course_entity__idx",
        "btree",
        False,
        False,
        ("course_id", "entity_id"),
        None,
    ),
    (
        "app.mastery_events",
        "mastery_events__user_entity_created__idx",
        "btree",
        False,
        False,
        ("user_id", "entity_id", "created_at"),
        None,
    ),
    ("app.mastery_events", "mastery_events_pkey", "btree", True, True, ("id",), None),
    (
        "app.problem_attempts",
        "problem_attempts__activity_created__idx",
        "btree",
        False,
        False,
        ("learning_activity_id", "created_at"),
        None,
    ),
    (
        "app.problem_attempts",
        "problem_attempts__learner_retry__idx",
        "btree",
        False,
        False,
        ("learner_update_next_attempt_at",),
        "learner_update_pending",
    ),
    (
        "app.problem_attempts",
        "problem_attempts__problem_id__idx",
        "btree",
        False,
        False,
        ("problem_id",),
        None,
    ),
    (
        "app.problem_attempts",
        "problem_attempts__user_course__idx",
        "btree",
        False,
        False,
        ("user_id", "course_id"),
        None,
    ),
    ("app.problem_attempts", "problem_attempts_pkey", "btree", True, True, ("id",), None),
    (
        "app.problems",
        "problems__concept_tier_difficulty__idx",
        "btree",
        False,
        False,
        ("concept_id", "tier", "difficulty"),
        None,
    ),
    (
        "app.problems",
        "problems__course_quarantined__idx",
        "btree",
        False,
        False,
        ("course_id", "quarantined_at"),
        None,
    ),
    (
        "app.problems",
        "problems_concept_id_problem_code_key",
        "btree",
        True,
        False,
        ("concept_id", "problem_code"),
        None,
    ),
    ("app.problems", "problems_pkey", "btree", True, True, ("id",), None),
    (
        "app.provisioning_runs",
        "provisioning_runs__authored_course_set__uidx",
        "btree",
        True,
        False,
        ("course_id", "set_index"),
        "(kind = 'authored_set'::text)",
    ),
    (
        "app.provisioning_runs",
        "provisioning_runs__course_concept__idx",
        "btree",
        False,
        False,
        ("course_id", "concept_id"),
        None,
    ),
    (
        "app.provisioning_runs",
        "provisioning_runs__ingest_run_id__idx",
        "btree",
        False,
        False,
        ("ingest_run_id",),
        None,
    ),
    (
        "app.provisioning_runs",
        "provisioning_runs__problem_document__idx",
        "btree",
        False,
        False,
        ("problem_document_id",),
        None,
    ),
    (
        "app.provisioning_runs",
        "provisioning_runs__solution_document__idx",
        "btree",
        False,
        False,
        ("solution_document_id",),
        None,
    ),
    ("app.provisioning_runs", "provisioning_runs_pkey", "btree", True, True, ("id",), None),
    (
        "app.question_opportunities",
        "question_opportunities__activity_id__idx",
        "btree",
        False,
        False,
        ("learning_activity_id",),
        None,
    ),
    (
        "app.question_opportunities",
        "question_opportunities__course_session__idx",
        "btree",
        False,
        False,
        ("course_id", "learning_activity_id"),
        None,
    ),
    (
        "app.question_opportunities",
        "question_opportunities_attempt_id_reference_node_id_key",
        "btree",
        True,
        False,
        ("attempt_id", "reference_node_id"),
        None,
    ),
    (
        "app.question_opportunities",
        "question_opportunities_pkey",
        "btree",
        True,
        True,
        ("id",),
        None,
    ),
    (
        "app.student_progress",
        "student_progress_pkey",
        "btree",
        True,
        True,
        ("user_id", "course_id"),
        None,
    ),
    (
        "app.tutoring_messages",
        "tutoring_messages__attempt_id__idx",
        "btree",
        False,
        False,
        ("attempt_id",),
        None,
    ),
    (
        "app.tutoring_messages",
        "tutoring_messages__course_activity__idx",
        "btree",
        False,
        False,
        ("course_id", "learning_activity_id"),
        None,
    ),
    (
        "app.tutoring_messages",
        "tutoring_messages_learning_activity_id_turn_index_key",
        "btree",
        True,
        False,
        ("learning_activity_id", "turn_index"),
        None,
    ),
    ("app.tutoring_messages", "tutoring_messages_pkey", "btree", True, True, ("id",), None),
    ("app.uploads", "uploads__document_id__idx", "btree", False, False, ("document_id",), None),
    (
        "app.uploads",
        "uploads__latest_course_week_kind__uidx",
        "btree",
        True,
        False,
        ("course_id", "week", "kind"),
        "is_latest",
    ),
    (
        "app.uploads",
        "uploads__uploaded_by_course__idx",
        "btree",
        False,
        False,
        ("uploaded_by", "course_id"),
        None,
    ),
    ("app.uploads", "uploads_pkey", "btree", True, True, ("id",), None),
    (
        "internal.chat_routing_decisions",
        "chat_routing_decisions__course_session__idx",
        "btree",
        False,
        False,
        ("course_id", "learning_activity_id"),
        None,
    ),
    (
        "internal.chat_routing_decisions",
        "chat_routing_decisions_pkey",
        "btree",
        True,
        True,
        ("id",),
        None,
    ),
    (
        "internal.chat_session_snippets",
        "chat_session_snippets__chunk_id__idx",
        "btree",
        False,
        False,
        ("chunk_id",),
        None,
    ),
    (
        "internal.chat_session_snippets",
        "chat_session_snippets__course_session__idx",
        "btree",
        False,
        False,
        ("course_id", "learning_activity_id"),
        None,
    ),
    (
        "internal.chat_session_snippets",
        "chat_session_snippets_pkey",
        "btree",
        True,
        True,
        ("learning_activity_id", "chunk_id"),
        None,
    ),
    (
        "internal.content_ingest_errors",
        "content_ingest_errors__course_run__idx",
        "btree",
        False,
        False,
        ("course_id", "ingest_run_id"),
        None,
    ),
    (
        "internal.content_ingest_errors",
        "content_ingest_errors_pkey",
        "btree",
        True,
        True,
        ("id",),
        None,
    ),
    (
        "internal.content_ingest_runs",
        "content_ingest_runs__course_document__idx",
        "btree",
        False,
        False,
        ("course_id", "document_id"),
        None,
    ),
    (
        "internal.content_ingest_runs",
        "content_ingest_runs_pkey",
        "btree",
        True,
        True,
        ("id",),
        None,
    ),
    (
        "internal.dedup_decisions",
        "dedup_decisions__concept_id__idx",
        "btree",
        False,
        False,
        ("concept_id",),
        None,
    ),
    (
        "internal.dedup_decisions",
        "dedup_decisions__course_run__idx",
        "btree",
        False,
        False,
        ("course_id", "ingest_run_id"),
        None,
    ),
    (
        "internal.dedup_decisions",
        "dedup_decisions__matched_entity_id__idx",
        "btree",
        False,
        False,
        ("matched_entity_id",),
        None,
    ),
    ("internal.dedup_decisions", "dedup_decisions_pkey", "btree", True, True, ("id",), None),
    (
        "internal.document_chunks",
        "document_chunks__content_fts__idx",
        "gin",
        False,
        False,
        ("to_tsvector('english'::regconfig, content)",),
        None,
    ),
    (
        "internal.document_chunks",
        "document_chunks__course_document__idx",
        "btree",
        False,
        False,
        ("course_id", "document_id"),
        None,
    ),
    (
        "internal.document_chunks",
        "document_chunks__document_page__idx",
        "btree",
        False,
        False,
        ("document_id", "page_number"),
        None,
    ),
    (
        "internal.document_chunks",
        "document_chunks__embedding_halfvec_hnsw__idx",
        "hnsw",
        False,
        False,
        ("(embedding::extensions.halfvec(3072))",),
        None,
    ),
    ("internal.document_chunks", "document_chunks_pkey", "btree", True, True, ("id",), None),
    (
        "internal.entity_prerequisites",
        "entity_prerequisites__course_from__idx",
        "btree",
        False,
        False,
        ("course_id", "from_entity_id"),
        None,
    ),
    (
        "internal.entity_prerequisites",
        "entity_prerequisites__to_entity_id__idx",
        "btree",
        False,
        False,
        ("to_entity_id",),
        None,
    ),
    (
        "internal.entity_prerequisites",
        "entity_prerequisites_pkey",
        "btree",
        True,
        True,
        ("from_entity_id", "to_entity_id"),
        None,
    ),
    (
        "internal.grading_runs",
        "grading_runs__attempt_created__idx",
        "btree",
        False,
        False,
        ("attempt_id", "created_at"),
        None,
    ),
    (
        "internal.grading_runs",
        "grading_runs__concept_id__idx",
        "btree",
        False,
        False,
        ("concept_id",),
        None,
    ),
    (
        "internal.grading_runs",
        "grading_runs__course_user__idx",
        "btree",
        False,
        False,
        ("course_id", "user_id"),
        None,
    ),
    (
        "internal.grading_runs",
        "grading_runs__problem_id__idx",
        "btree",
        False,
        False,
        ("problem_id",),
        None,
    ),
    (
        "internal.grading_runs",
        "grading_runs__user_course__idx",
        "btree",
        False,
        False,
        ("user_id", "course_id"),
        None,
    ),
    (
        "internal.grading_runs",
        "grading_runs_attempt_id_role_grader_version_key",
        "btree",
        True,
        False,
        ("attempt_id", "role", "grader_version"),
        None,
    ),
    ("internal.grading_runs", "grading_runs_pkey", "btree", True, True, ("id",), None),
    (
        "internal.ingest_page_evidence",
        "ingest_page_evidence__course_run__idx",
        "btree",
        False,
        False,
        ("course_id", "ingest_run_id"),
        None,
    ),
    (
        "internal.ingest_page_evidence",
        "ingest_page_evidence__document_id__idx",
        "btree",
        False,
        False,
        ("document_id",),
        None,
    ),
    (
        "internal.ingest_page_evidence",
        "ingest_page_evidence_pkey",
        "btree",
        True,
        True,
        ("id",),
        None,
    ),
    (
        "internal.upload_jobs",
        "upload_jobs__course_upload__idx",
        "btree",
        False,
        False,
        ("course_id", "upload_id"),
        None,
    ),
    (
        "internal.upload_jobs",
        "upload_jobs__dequeue__idx",
        "btree",
        False,
        False,
        ("state", "lease_expires_at", "created_at"),
        None,
    ),
    ("internal.upload_jobs", "upload_jobs_pkey", "btree", True, True, ("id",), None),
}

# Named, pinned known-gap set (see module docstring). Any FK not in this set
# must be leftmost-prefix covered by some index, or this test fails loudly.
KNOWN_UNCOVERED_FKS: set[str] = {
    "ai_usage_reports_course_id_fkey",
    "course_invites_course_id_fkey",
    "learner_state_course_id_fkey",
    "learning_activities__concept_id__fkey",
    "learning_activities_course_id_fkey",
    "mastery_events_entity_id_fkey",
    "problem_attempts_course_id_fkey",
    "provisioning_runs_concept_id_fkey",
    "student_progress_course_id_fkey",
    "chat_routing_decisions_learning_activity_id_fkey",
    "content_ingest_errors_ingest_run_id_fkey",
    "content_ingest_runs_document_id_fkey",
    "dedup_decisions_ingest_run_id_fkey",
    "ingest_page_evidence_ingest_run_id_fkey",
    "upload_jobs_upload_id_fkey",
}

_INDEX_QUERY = """
SELECT
    n.nspname AS schema_name,
    c.relname AS table_name,
    ic.relname AS index_name,
    am.amname AS access_method,
    i.indisunique AS is_unique,
    i.indisprimary AS is_primary,
    (
        SELECT array_agg(pg_get_indexdef(i.indexrelid, k, true) ORDER BY k)
        FROM generate_series(1, i.indnkeyatts) AS k
    ) AS key_defs,
    pg_get_expr(i.indpred, i.indrelid) AS predicate
FROM pg_index i
JOIN pg_class c ON c.oid = i.indrelid
JOIN pg_class ic ON ic.oid = i.indexrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_am am ON am.oid = ic.relam
WHERE n.nspname IN ('app', 'internal')
ORDER BY n.nspname, c.relname, ic.relname
"""

_FK_QUERY = """
SELECT
    n.nspname AS schema_name,
    c.relname AS table_name,
    con.conname AS constraint_name,
    (
        SELECT array_agg(a.attname ORDER BY ord.ordinality)
        FROM unnest(con.conkey) WITH ORDINALITY AS ord(attnum, ordinality)
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ord.attnum
    ) AS fk_columns
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE con.contype = 'f' AND n.nspname IN ('app', 'internal')
ORDER BY n.nspname, c.relname, con.conname
"""


def _dsn(url: str, database: str) -> str:
    return (
        make_url(url)
        .set(drivername="postgresql", database=database)
        .render_as_string(hide_password=False)
    )


@pytest.fixture(scope="session")
def index_allowlist_dsn(request: pytest.FixtureRequest):
    local_override = os.getenv("DB17_LOCAL_TEST_DATABASE_URL")
    pg_url = local_override or request.getfixturevalue("_pg_url")
    parsed_url = make_url(pg_url)
    if local_override:
        assert parsed_url.host in {"127.0.0.1", "localhost", "::1"}, (
            "DB17_LOCAL_TEST_DATABASE_URL must target loopback"
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
async def schema_conn(index_allowlist_dsn: str):
    conn = await asyncpg.connect(index_allowlist_dsn)
    try:
        yield conn
    finally:
        await conn.close()


async def _fetch_actual_indexes(
    conn: asyncpg.Connection,
) -> set[tuple[str, str, str, bool, bool, tuple[str, ...], str | None]]:
    rows = await conn.fetch(_INDEX_QUERY)
    return {
        (
            f"{row['schema_name']}.{row['table_name']}",
            row["index_name"],
            row["access_method"],
            row["is_unique"],
            row["is_primary"],
            tuple(row["key_defs"]),
            row["predicate"],
        )
        for row in rows
    }


@pytest.mark.asyncio
async def test_complete_index_inventory_matches_pinned_allowlist_exactly(schema_conn):
    actual = await _fetch_actual_indexes(schema_conn)

    missing = EXPECTED_INDEXES - actual
    extra = actual - EXPECTED_INDEXES
    assert not missing and not extra, (
        f"index inventory drifted from the DB-17 pinned allowlist:\n"
        f"  missing (expected but not found): {sorted(missing)}\n"
        f"  extra (found but not expected): {sorted(extra)}"
    )


@pytest.mark.asyncio
async def test_no_duplicate_indexes_on_any_table(schema_conn):
    """No two indexes on the same table share access method + key list + predicate.

    This is the invariant plan section 6.1 exists to guarantee going forward:
    the target schema is an allowlist, not an accumulation of redundant
    indexes like the legacy 13 duplicate pairs this task also removes.
    """
    actual = await _fetch_actual_indexes(schema_conn)
    groups: dict[tuple[str, str, tuple[str, ...], str | None], list[str]] = {}
    for table, name, am, _unique, _primary, keys, predicate in actual:
        groups.setdefault((table, am, keys, predicate), []).append(name)

    duplicates = {key: names for key, names in groups.items() if len(names) > 1}
    assert not duplicates, (
        f"duplicate indexes found (table/am/keys/predicate -> names): {duplicates}"
    )


@pytest.mark.asyncio
async def test_fk_coverage_matches_pinned_known_gap_set(schema_conn):
    """Every FK is leftmost-prefix covered by an index, or is a pinned known gap.

    Computes coverage dynamically against the real catalog (not hardcoded),
    so this test keeps working if indexes are added/removed later -- it only
    fails if the SET of uncovered FKs changes without ``KNOWN_UNCOVERED_FKS``
    being consciously updated to match (a newly introduced uncovered FK, or a
    fixed one that should be removed from the known-gap list).
    """
    actual_indexes = await _fetch_actual_indexes(schema_conn)
    table_keys: dict[str, list[tuple[str, ...]]] = {}
    for table, _name, _am, _unique, _primary, keys, _predicate in actual_indexes:
        table_keys.setdefault(table, []).append(keys)

    fk_rows = await schema_conn.fetch(_FK_QUERY)
    live_uncovered: set[str] = set()
    all_fk_names: set[str] = set()
    for row in fk_rows:
        table = f"{row['schema_name']}.{row['table_name']}"
        fk_cols = tuple(row["fk_columns"])
        all_fk_names.add(row["constraint_name"])
        covered = any(keys[: len(fk_cols)] == fk_cols for keys in table_keys.get(table, []))
        if not covered:
            live_uncovered.add(row["constraint_name"])

    # Every pinned known-gap name must actually exist as a live FK (catches a
    # stale/renamed entry in KNOWN_UNCOVERED_FKS).
    unknown_pins = KNOWN_UNCOVERED_FKS - all_fk_names
    assert not unknown_pins, f"KNOWN_UNCOVERED_FKS names FKs that no longer exist: {unknown_pins}"

    newly_uncovered = live_uncovered - KNOWN_UNCOVERED_FKS
    now_covered = KNOWN_UNCOVERED_FKS - live_uncovered
    assert not newly_uncovered and not now_covered, (
        "FK coverage drifted from the DB-17 pinned known-gap set:\n"
        f"  newly uncovered (add an index or pin here with a reason): {sorted(newly_uncovered)}\n"
        f"  now covered (remove from KNOWN_UNCOVERED_FKS, the gap is closed): {sorted(now_covered)}"
    )
