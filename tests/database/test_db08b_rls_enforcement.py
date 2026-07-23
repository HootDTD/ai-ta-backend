"""DB-08b: real-Postgres RLS enforcement tests.

The security review of feat/db08b-rls-enforcement found the RLS-enforcement
runtime (database/session.py) shipped with zero automated verification --
the shared Testcontainers harness (tests/conftest.py::_pg_url) builds schema
via Base.metadata.create_all and never applies the migration SQL, so it
creates no app_runtime role, no RLS policies, and no grants, making it
structurally incapable of exercising this code.

This module builds a FRESH database on that same Docker Postgres server,
applies the real migration chain (auth shim -> legacy snapshot -> DB-04
create_app_schema_v1 -> DB-06 retrieval_functions_v1 -> DB-08b's own grants
migration), seeds two tenants (course A / course B) across every app table
the 32 policies cover, and proves, end to end, against real asyncpg/asyncpg-
via-SQLAlchemy connections:

1. ``app_runtime`` binds to all 32 app-schema policies exactly like
   ``authenticated`` does (the DB-08b membership grant), with an
   own/cross-tenant/no-claims/owner-bypass matrix.
2. The session-creation-path enforcement boundary: ``get_db_session``
   enforces; ``get_async_session`` NEVER does, even with
   ``current_request_user_id`` set in the calling context (the exact shape
   of a FastAPI BackgroundTasks callback continuing a request's Task).
3. Cross-transaction / pooled-connection isolation: a pooled connection
   never leaks role or claims from one transaction into the next.
4. The internal-schema grants this migration adds are exactly sufficient
   for the real request-path callsites (the three retrieval RPCs, plus one
   representative probe per granted internal table).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import apollo.auth_deps as auth_deps_module
import database.session as session_module
from apollo.auth_deps import require_session_owner, require_user
from auth import AuthContext
from database.models import Course
from database.session import current_request_user_id, get_async_session, get_db_session

pytestmark = pytest.mark.integration

_REPO = Path(__file__).resolve().parents[2]
_MIGRATIONS = _REPO / "supabase" / "migrations"
_SNAPSHOT = _MIGRATIONS / "20260717032246_legacy_public_snapshot.sql"
_CREATE = _MIGRATIONS / "20260717035041_create_app_schema_v1.sql"
_RETRIEVAL = _MIGRATIONS / "20260717050000_retrieval_functions_v1.sql"
_GRANTS = _MIGRATIONS / "20260722120000_db08b_rls_enforcement_grants.sql"
_DB_NAME = "db08b_rls_enforcement"

STUDENT_A = "50000000-0000-4000-8000-00000000000a"
STUDENT_B = "60000000-0000-4000-8000-00000000000b"
TEACHER_A = "70000000-0000-4000-8000-00000000000c"
NO_MEMBERSHIP = "80000000-0000-4000-8000-00000000000d"

# Same Supabase Auth role/function shim used by the DB-04/DB-06 real-Postgres
# tests (tests/database/test_app_schema_v1.py, test_retrieval_functions_v1.py).
# service_role is required by retrieval_functions_v1's own preconditions.
_AUTH_BOOTSTRAP = """
DO $roles$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    CREATE ROLE anon NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
    CREATE ROLE authenticated NOLOGIN NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
    CREATE ROLE service_role NOLOGIN BYPASSRLS;
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


def _admin_dsn(url: str, database: str) -> str:
    return make_url(url).set(drivername="postgresql", database=database).render_as_string(
        hide_password=False
    )


def _sqlalchemy_dsn(url: str, database: str) -> str:
    """asyncpg-driver SQLAlchemy URL -- what SUPABASE_DB_URL looks like in prod."""
    return make_url(url).set(
        drivername="postgresql+asyncpg", database=database
    ).render_as_string(hide_password=False)


async def _seed(conn: asyncpg.Connection) -> None:
    """Seed two tenants (course A / course B) across every app table the 32
    policies cover, plus enough internal-table rows to exercise the DB-08b
    request-path grants. Runs over an admin (superuser) connection, so RLS
    never interferes with seeding."""
    await conn.executemany(
        "INSERT INTO auth.users (id) VALUES ($1)",
        [(STUDENT_A,), (STUDENT_B,), (TEACHER_A,), (NO_MEMBERSHIP,)],
    )

    course_a = await conn.fetchval(
        "INSERT INTO app.courses (name, slug, subject_name) "
        "VALUES ('Course A', 'course-one', 'Physics') RETURNING id"
    )
    course_b = await conn.fetchval(
        "INSERT INTO app.courses (name, slug, subject_name) "
        "VALUES ('Course B', 'course-two', 'Math') RETURNING id"
    )
    await conn.executemany(
        "INSERT INTO app.course_memberships (user_id, course_id, role) VALUES ($1, $2, $3)",
        [
            (STUDENT_A, course_a, "student"),
            (TEACHER_A, course_a, "teacher"),
            (STUDENT_B, course_b, "student"),
        ],
    )

    ids: dict[str, dict[str, int]] = {"a": {"course": course_a}, "b": {"course": course_b}}

    for tag, course_id, user_id in (("a", course_a, STUDENT_A), ("b", course_b, STUDENT_B)):
        doc_id = await conn.fetchval(
            "INSERT INTO app.documents (course_id, title, content, content_hash) "
            "VALUES ($1, $2, 'body', $3) RETURNING id",
            course_id,
            f"doc-{tag}",
            f"hash-{tag}",
        )
        concept_id = await conn.fetchval(
            "INSERT INTO app.concepts "
            "(course_id, subject_slug, subject_display_name, slug, display_name) "
            "VALUES ($1, 'physics', 'Physics', $2, $2) RETURNING id",
            course_id,
            f"concept-{tag}",
        )
        problem_id = await conn.fetchval(
            "INSERT INTO app.problems "
            "(course_id, concept_id, problem_code, difficulty, problem_text, reference_solution) "
            "VALUES ($1, $2, $3, 'standard', 'solve for x', '{}'::jsonb) RETURNING id",
            course_id,
            concept_id,
            f"problem-{tag}",
        )
        entity_id = await conn.fetchval(
            "INSERT INTO app.learner_entities "
            "(course_id, concept_id, canonical_key, kind, display_name) "
            "VALUES ($1, $2, $3, 'variable', $3) RETURNING id",
            course_id,
            concept_id,
            f"entity-{tag}",
        )
        chat_id = await conn.fetchval(
            "INSERT INTO app.learning_activities "
            "(modality, external_id, user_id, course_id, status) "
            "VALUES ('chat', $1, $2, $3, 'active') RETURNING id",
            f"chat-{tag}",
            user_id,
            course_id,
        )
        tutoring_id = await conn.fetchval(
            "INSERT INTO app.learning_activities "
            "(modality, user_id, course_id, status, phase) "
            "VALUES ('tutoring', $1, $2, 'active', 'TEACHING') RETURNING id",
            user_id,
            course_id,
        )
        await conn.execute(
            "INSERT INTO app.chat_messages "
            "(course_id, learning_activity_id, turn_index, external_id, role, content) "
            "VALUES ($1, $2, 0, $3, 'user', 'hi')",
            course_id,
            chat_id,
            f"chat-msg-{tag}",
        )
        attempt_id = await conn.fetchval(
            "INSERT INTO app.problem_attempts "
            "(course_id, user_id, learning_activity_id, problem_id, difficulty) "
            "VALUES ($1, $2, $3, $4, 'standard') RETURNING id",
            course_id,
            user_id,
            tutoring_id,
            problem_id,
        )
        await conn.execute(
            "INSERT INTO app.tutoring_messages "
            "(course_id, learning_activity_id, attempt_id, role, content, turn_index) "
            "VALUES ($1, $2, $3, 'student', 'hello', 0)",
            course_id,
            tutoring_id,
            attempt_id,
        )
        await conn.execute(
            "INSERT INTO app.question_opportunities "
            "(course_id, learning_activity_id, attempt_id, reference_node_id) "
            "VALUES ($1, $2, $3, $4)",
            course_id,
            tutoring_id,
            attempt_id,
            f"node-{tag}",
        )
        await conn.execute(
            "INSERT INTO app.student_progress (user_id, course_id) VALUES ($1, $2)",
            user_id,
            course_id,
        )
        await conn.execute(
            "INSERT INTO app.learner_state "
            "(user_id, course_id, entity_id, belief, mastery, confidence) "
            "VALUES ($1, $2, $3, ARRAY[0.34,0.33,0.33]::real[], 0.5, 0.5)",
            user_id,
            course_id,
            entity_id,
        )
        await conn.execute(
            "INSERT INTO app.mastery_events "
            "(user_id, course_id, entity_id, attempt_id, event_kind, "
            " prior_belief, posterior_belief, mastery_after) "
            "VALUES ($1, $2, $3, $4, 'graded', "
            " ARRAY[0.34,0.33,0.33]::real[], ARRAY[0.5,0.3,0.2]::real[], 0.5)",
            user_id,
            course_id,
            entity_id,
            attempt_id,
        )
        await conn.execute(
            "INSERT INTO app.ai_usage_reports (id, user_id, course_id, chat_id, jsonld) "
            "VALUES (gen_random_uuid(), $1, $2, $3, '{}'::jsonb)",
            user_id,
            course_id,
            f"chat-{tag}",
        )
        ingest_run_id = await conn.fetchval(
            "INSERT INTO internal.content_ingest_runs (course_id, document_id) "
            "VALUES ($1, $2) RETURNING id",
            course_id,
            doc_id,
        )
        await conn.execute(
            "INSERT INTO internal.ingest_page_evidence "
            "(course_id, ingest_run_id, document_id, role, page_number, ocr_text) "
            "VALUES ($1, $2, $3, 'problem', 1, 'ocr text')",
            course_id,
            ingest_run_id,
            doc_id,
        )
        await conn.execute(
            "INSERT INTO internal.grading_runs "
            "(course_id, user_id, attempt_id, problem_id, role, grader_used, grader_version) "
            "VALUES ($1, $2, $3, $4, 'canonical', 'llm_fallback', 'v1')",
            course_id,
            user_id,
            attempt_id,
            problem_id,
        )
        chunk_id = await conn.fetchval(
            "INSERT INTO internal.document_chunks (course_id, document_id, content) "
            "VALUES ($1, $2, 'chunk text') RETURNING id",
            course_id,
            doc_id,
        )
        ids[tag].update(
            document=doc_id,
            concept=concept_id,
            problem=problem_id,
            entity=entity_id,
            chat=chat_id,
            tutoring=tutoring_id,
            attempt=attempt_id,
            ingest_run=ingest_run_id,
            chunk=chunk_id,
        )

    await conn.execute(
        "INSERT INTO app.course_invites (code, course_id, role, created_by) "
        "VALUES ('invite-a', $1, 'student', $2)",
        course_a,
        TEACHER_A,
    )
    await conn.execute(
        "INSERT INTO app.uploads (course_id, week, kind, title) "
        "VALUES ($1, 1, 'notes', 'week 1 notes')",
        course_a,
    )
    await conn.execute(
        "INSERT INTO app.provisioning_runs (course_id, kind, set_index) "
        "VALUES ($1, 'authored_set', 1)",
        course_a,
    )
    # A second entity-prerequisite pair scoped to course A (for INSERT probes
    # under app_runtime -- course A only, since STUDENT_A owns no course-B row).
    entity_a2 = await conn.fetchval(
        "INSERT INTO app.learner_entities "
        "(course_id, concept_id, canonical_key, kind, display_name) "
        "VALUES ($1, $2, 'entity-a2', 'variable', 'entity-a2') RETURNING id",
        course_a,
        ids["a"]["concept"],
    )
    ids["a"]["entity2"] = entity_a2


@pytest.fixture(scope="session")
def db08b_dsns(request: pytest.FixtureRequest) -> tuple[str, str]:
    """Fresh DB with the DB-04 + DB-06 + DB-08b chain applied, plus seed data.

    Returns (admin_dsn, sqlalchemy_asyncpg_dsn).
    """
    pg_url = request.getfixturevalue("_pg_url")
    base_database = make_url(pg_url).database
    assert base_database
    admin_dsn = _admin_dsn(pg_url, base_database)
    test_dsn = _admin_dsn(pg_url, _DB_NAME)

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
            await conn.execute(_RETRIEVAL.read_text(encoding="utf-8"))
            await conn.execute(_GRANTS.read_text(encoding="utf-8"))
            await _seed(conn)
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
        yield test_dsn, _sqlalchemy_dsn(pg_url, _DB_NAME)
    finally:
        asyncio.run(teardown())


@pytest_asyncio.fixture
async def schema_conn(db08b_dsns: tuple[str, str]):
    conn = await asyncpg.connect(db08b_dsns[0])
    try:
        yield conn
    finally:
        await conn.close()


async def _set_app_runtime(conn: asyncpg.Connection, user_id: str) -> None:
    await conn.execute("SET LOCAL ROLE app_runtime")
    await conn.execute(
        "SELECT set_config('request.jwt.claims', $1, true)",
        f'{{"sub":"{user_id}","role":"authenticated"}}',
    )


async def _set_app_runtime_no_claims(conn: asyncpg.Connection) -> None:
    await conn.execute("SET LOCAL ROLE app_runtime")


async def _lookup_ids(conn: asyncpg.Connection) -> dict[str, object]:
    """Look up fixture ids as the connection's default (owner/BYPASSRLS)
    role -- MUST be called outside any role-switched transaction (i.e.
    between tx.rollback() and the next tx.start()), so every id is visible
    regardless of which RLS policy a later probe is proving. Using a
    cross-tenant-visible id here (rather than looking it up again under a
    denied role's own claims, which would silently return NULL and turn an
    intended RLS-denial assertion into a NOT NULL constraint violation
    instead) is what makes the denial probes below test the WRITE policy,
    not the read visibility of the id itself.
    """
    return {
        "course_a": await conn.fetchval("SELECT id FROM app.courses WHERE slug = 'course-one'"),
        "course_b": await conn.fetchval("SELECT id FROM app.courses WHERE slug = 'course-two'"),
        "chat_a": await conn.fetchval("SELECT id FROM app.learning_activities WHERE external_id = 'chat-a'"),
        "tutoring_a": await conn.fetchval(
            "SELECT id FROM app.learning_activities WHERE user_id = $1 AND modality = 'tutoring'",
            STUDENT_A,
        ),
        "attempt_a": await conn.fetchval(
            "SELECT id FROM app.problem_attempts WHERE user_id = $1", STUDENT_A
        ),
    }


# ===========================================================================
# 1. The 32-policy matrix, as app_runtime (proves the DB-08b membership grant
#    binds app_runtime to every policy exactly like `authenticated`).
# ===========================================================================


@pytest.mark.asyncio
async def test_app_runtime_bound_to_all_32_app_policies(schema_conn):
    """Precondition sanity check: the grants migration's own postcondition
    plus a direct count -- app_runtime is a member of authenticated, and the
    policy surface is still exactly 32 (DB-08b adds zero policies)."""
    is_member = await schema_conn.fetchval(
        "SELECT pg_has_role('app_runtime', 'authenticated', 'MEMBER')"
    )
    assert is_member is True
    policy_count = await schema_conn.fetchval(
        "SELECT count(*) FROM pg_policies WHERE schemaname = 'app'"
    )
    assert policy_count == 32


# (table, own_sql, cross_sql, write_sql_or_None) -- own_sql/cross_sql return a
# row count under SET LOCAL ROLE app_runtime with STUDENT_A's / STUDENT_B's
# claims respectively; write_sql, when present, is attempted as STUDENT_A and
# must succeed for A's own row and be re-attempted as STUDENT_B and denied
# (0 rows affected) against A's row.
_MEMBER_SELECT_POLICIES = [
    # (policy name(s) covered, table, id column, id value under course A)
    ("courses__member_select", "app.courses", "slug", "course-one"),
    ("documents__member_select", "app.documents", "title", "doc-a"),
    ("concepts__member_select", "app.concepts", "slug", "concept-a"),
    ("problems__member_select", "app.problems", "problem_code", "problem-a"),
    ("learner_entities__member_select", "app.learner_entities", "canonical_key", "entity-a"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("policy", "table", "col", "value"), _MEMBER_SELECT_POLICIES)
async def test_member_select_policies_bind_under_app_runtime(
    schema_conn, policy, table, col, value
):
    """Course-A student and teacher both see the course-A row; course-B
    student (no membership in A) does not."""
    for user_id, expected in ((STUDENT_A, 1), (TEACHER_A, 1), (STUDENT_B, 0)):
        tx = schema_conn.transaction()
        await tx.start()
        try:
            await _set_app_runtime(schema_conn, user_id)
            count = await schema_conn.fetchval(
                f"SELECT count(*) FROM {table} WHERE {col} = $1", value
            )
            assert count == expected, f"{policy} user={user_id} table={table}"
        finally:
            await tx.rollback()


_TEACHER_UPDATE_POLICIES = [
    ("courses__teacher_update", "app.courses", "name", "slug", "course-one"),
    ("documents__teacher_update", "app.documents", "title", "title", "doc-a"),
    ("concepts__teacher_update", "app.concepts", "display_name", "slug", "concept-a"),
    ("problems__teacher_update", "app.problems", "problem_text", "problem_code", "problem-a"),
    (
        "learner_entities__teacher_update",
        "app.learner_entities",
        "display_name",
        "canonical_key",
        "entity-a",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("policy", "table", "set_col", "where_col", "value"), _TEACHER_UPDATE_POLICIES)
async def test_teacher_update_policies_bind_under_app_runtime(
    schema_conn, policy, table, set_col, where_col, value
):
    """Course-A teacher can UPDATE a course-A row; course-A student and
    course-B student (cross-tenant) cannot."""
    for user_id, expected_rowcount in ((TEACHER_A, 1), (STUDENT_A, 0), (STUDENT_B, 0)):
        tx = schema_conn.transaction()
        await tx.start()
        try:
            await _set_app_runtime(schema_conn, user_id)
            new_value = f"{value}-edited-by-{user_id[:8]}"
            result_id = await schema_conn.fetchval(
                f"UPDATE {table} SET {set_col} = $1 WHERE {where_col} = $2 "
                f"AND {set_col} <> $1 RETURNING 1",
                new_value,
                value,
            )
            rowcount = 1 if result_id is not None else 0
            assert rowcount == expected_rowcount, f"{policy} user={user_id}"
        finally:
            await tx.rollback()


@pytest.mark.asyncio
async def test_course_memberships_self_or_teacher_select(schema_conn):
    """A student sees their own membership row; a teacher sees every
    membership in their course; a student never sees another course's rows."""
    for user_id, expected_min in ((STUDENT_A, 1), (TEACHER_A, 2)):
        tx = schema_conn.transaction()
        await tx.start()
        try:
            await _set_app_runtime(schema_conn, user_id)
            count = await schema_conn.fetchval(
                "SELECT count(*) FROM app.course_memberships mem "
                "JOIN app.courses c ON c.id = mem.course_id WHERE c.slug = 'course-one'"
            )
            assert count >= expected_min
        finally:
            await tx.rollback()

    tx = schema_conn.transaction()
    await tx.start()
    try:
        await _set_app_runtime(schema_conn, STUDENT_B)
        count = await schema_conn.fetchval(
            "SELECT count(*) FROM app.course_memberships mem "
            "JOIN app.courses c ON c.id = mem.course_id WHERE c.slug = 'course-one'"
        )
        assert count == 0
    finally:
        await tx.rollback()


_TEACHER_ALL_POLICIES = [
    ("course_invites__teacher_all", "app.course_invites", "code", "invite-a"),
    ("uploads__teacher_all", "app.uploads", "title", "week 1 notes"),
    ("provisioning_runs__teacher_all", "app.provisioning_runs", "set_index", 1),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("policy", "table", "col", "value"), _TEACHER_ALL_POLICIES)
async def test_teacher_all_policies_bind_under_app_runtime(schema_conn, policy, table, col, value):
    """Teacher of course A sees the row; student of A and student of B (cross-
    tenant) both see nothing -- these tables are teacher-only, no student
    read policy exists."""
    for user_id, expected in ((TEACHER_A, 1), (STUDENT_A, 0), (STUDENT_B, 0)):
        tx = schema_conn.transaction()
        await tx.start()
        try:
            await _set_app_runtime(schema_conn, user_id)
            count = await schema_conn.fetchval(f"SELECT count(*) FROM {table} WHERE {col} = $1", value)
            assert count == expected, f"{policy} user={user_id}"
        finally:
            await tx.rollback()


@pytest.mark.asyncio
async def test_learning_activities_owner_all_and_teacher_tutoring_select(schema_conn):
    """learning_activities__owner_all: the owning student sees/updates their
    own activity, a different student does not.
    learning_activities__teacher_tutoring_select: the course teacher can see
    a student's TUTORING activity (but this policy does not cover 'chat'
    modality -- verified separately below)."""
    # owner_all: STUDENT_A sees both their chat and tutoring rows.
    tx = schema_conn.transaction()
    await tx.start()
    try:
        await _set_app_runtime(schema_conn, STUDENT_A)
        count = await schema_conn.fetchval(
            "SELECT count(*) FROM app.learning_activities WHERE user_id = $1", STUDENT_A
        )
        assert count == 2
    finally:
        await tx.rollback()

    # cross-tenant: STUDENT_B sees none of STUDENT_A's rows.
    tx = schema_conn.transaction()
    await tx.start()
    try:
        await _set_app_runtime(schema_conn, STUDENT_B)
        count = await schema_conn.fetchval(
            "SELECT count(*) FROM app.learning_activities WHERE user_id = $1", STUDENT_A
        )
        assert count == 0
    finally:
        await tx.rollback()

    # teacher_tutoring_select: TEACHER_A sees STUDENT_A's tutoring activity.
    tx = schema_conn.transaction()
    await tx.start()
    try:
        await _set_app_runtime(schema_conn, TEACHER_A)
        count = await schema_conn.fetchval(
            "SELECT count(*) FROM app.learning_activities "
            "WHERE user_id = $1 AND modality = 'tutoring'",
            STUDENT_A,
        )
        assert count == 1
    finally:
        await tx.rollback()


@pytest.mark.asyncio
async def test_chat_messages_owner_select_and_insert(schema_conn):
    for user_id, expected in ((STUDENT_A, 1), (STUDENT_B, 0)):
        tx = schema_conn.transaction()
        await tx.start()
        try:
            await _set_app_runtime(schema_conn, user_id)
            count = await schema_conn.fetchval(
                "SELECT count(*) FROM app.chat_messages cm "
                "JOIN app.learning_activities la ON la.id = cm.learning_activity_id "
                "WHERE la.external_id = 'chat-a'"
            )
            assert count == expected
        finally:
            await tx.rollback()

    # owner_insert: STUDENT_A can insert into their own chat; STUDENT_B cannot.
    ids = await _lookup_ids(schema_conn)
    course_id, activity_id = ids["course_a"], ids["chat_a"]
    for user_id, should_succeed in ((STUDENT_A, True), (STUDENT_B, False)):
        tx = schema_conn.transaction()
        await tx.start()
        try:
            await _set_app_runtime(schema_conn, user_id)
            if should_succeed:
                new_id = await schema_conn.fetchval(
                    "INSERT INTO app.chat_messages "
                    "(course_id, learning_activity_id, turn_index, external_id, role, content) "
                    "VALUES ($1, $2, 1, 'chat-msg-a-2', 'user', 'again') RETURNING id",
                    course_id,
                    activity_id,
                )
                assert new_id is not None
            else:
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await schema_conn.execute(
                        "INSERT INTO app.chat_messages "
                        "(course_id, learning_activity_id, turn_index, external_id, role, content) "
                        "VALUES ($1, $2, 2, 'chat-msg-a-3', 'user', 'denied')",
                        course_id,
                        activity_id,
                    )
        finally:
            await tx.rollback()


@pytest.mark.asyncio
async def test_tutoring_messages_owner_or_teacher_select_insert_update(schema_conn):
    for user_id, expected in ((STUDENT_A, 1), (TEACHER_A, 1), (STUDENT_B, 0)):
        tx = schema_conn.transaction()
        await tx.start()
        try:
            await _set_app_runtime(schema_conn, user_id)
            count = await schema_conn.fetchval(
                "SELECT count(*) FROM app.tutoring_messages tm "
                "JOIN app.learning_activities la ON la.id = tm.learning_activity_id "
                "WHERE la.user_id = $1",
                STUDENT_A,
            )
            assert count == expected, f"select user={user_id}"
        finally:
            await tx.rollback()

    ids = await _lookup_ids(schema_conn)
    course_id, activity_id = ids["course_a"], ids["tutoring_a"]
    for user_id, should_succeed in ((STUDENT_A, True), (STUDENT_B, False)):
        tx = schema_conn.transaction()
        await tx.start()
        try:
            await _set_app_runtime(schema_conn, user_id)
            if should_succeed:
                new_id = await schema_conn.fetchval(
                    "INSERT INTO app.tutoring_messages "
                    "(course_id, learning_activity_id, role, content, turn_index) "
                    "VALUES ($1, $2, 'student', 'more', 1) RETURNING id",
                    course_id,
                    activity_id,
                )
                assert new_id is not None
                updated = await schema_conn.fetchval(
                    "UPDATE app.tutoring_messages SET content = 'edited' WHERE id = $1 RETURNING id",
                    new_id,
                )
                assert updated is not None
            else:
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await schema_conn.execute(
                        "INSERT INTO app.tutoring_messages "
                        "(course_id, learning_activity_id, role, content, turn_index) "
                        "VALUES ($1, $2, 'student', 'denied', 2)",
                        course_id,
                        activity_id,
                    )
        finally:
            await tx.rollback()


@pytest.mark.asyncio
async def test_problem_attempts_owner_or_teacher_select_insert_update(schema_conn):
    for user_id, expected in ((STUDENT_A, 1), (TEACHER_A, 1), (STUDENT_B, 0)):
        tx = schema_conn.transaction()
        await tx.start()
        try:
            await _set_app_runtime(schema_conn, user_id)
            count = await schema_conn.fetchval(
                "SELECT count(*) FROM app.problem_attempts WHERE user_id = $1", STUDENT_A
            )
            assert count == expected, f"select user={user_id}"
        finally:
            await tx.rollback()

    for user_id, should_succeed in ((STUDENT_A, True), (STUDENT_B, False)):
        tx = schema_conn.transaction()
        await tx.start()
        try:
            await _set_app_runtime(schema_conn, user_id)
            attempt_id = await schema_conn.fetchval(
                "SELECT id FROM app.problem_attempts WHERE user_id = $1", STUDENT_A
            )
            if should_succeed:
                updated = await schema_conn.fetchval(
                    "UPDATE app.problem_attempts SET result = 'solved' WHERE id = $1 RETURNING id",
                    attempt_id,
                )
                assert updated is not None
            else:
                updated = await schema_conn.fetchval(
                    "UPDATE app.problem_attempts SET result = 'solved' WHERE id = $1 RETURNING id",
                    attempt_id,
                )
                assert updated is None
        finally:
            await tx.rollback()


@pytest.mark.asyncio
async def test_question_opportunities_owner_or_teacher_select_insert_update(schema_conn):
    for user_id, expected in ((STUDENT_A, 1), (TEACHER_A, 1), (STUDENT_B, 0)):
        tx = schema_conn.transaction()
        await tx.start()
        try:
            await _set_app_runtime(schema_conn, user_id)
            count = await schema_conn.fetchval(
                "SELECT count(*) FROM app.question_opportunities qo "
                "JOIN app.problem_attempts pa ON pa.id = qo.attempt_id "
                "WHERE pa.user_id = $1",
                STUDENT_A,
            )
            assert count == expected, f"select user={user_id}"
        finally:
            await tx.rollback()

    for user_id, should_succeed in ((STUDENT_A, True), (STUDENT_B, False)):
        tx = schema_conn.transaction()
        await tx.start()
        try:
            await _set_app_runtime(schema_conn, user_id)
            qo_id = await schema_conn.fetchval(
                "SELECT qo.id FROM app.question_opportunities qo "
                "JOIN app.problem_attempts pa ON pa.id = qo.attempt_id "
                "WHERE pa.user_id = $1",
                STUDENT_A,
            )
            updated = (
                await schema_conn.fetchval(
                    "UPDATE app.question_opportunities SET state = 'answered' "
                    "WHERE id = $1 RETURNING id",
                    qo_id,
                )
                if qo_id is not None
                else None
            )
            assert (updated is not None) == should_succeed
        finally:
            await tx.rollback()

    # owner_insert: STUDENT_A can insert a new opportunity against their own
    # attempt; STUDENT_B (whose claims don't own attempt A) cannot.
    ids = await _lookup_ids(schema_conn)
    course_id, activity_id, attempt_id = ids["course_a"], ids["tutoring_a"], ids["attempt_a"]
    for user_id, should_succeed in ((STUDENT_A, True), (STUDENT_B, False)):
        tx = schema_conn.transaction()
        await tx.start()
        try:
            await _set_app_runtime(schema_conn, user_id)
            if should_succeed:
                new_id = await schema_conn.fetchval(
                    "INSERT INTO app.question_opportunities "
                    "(course_id, learning_activity_id, attempt_id, reference_node_id) "
                    "VALUES ($1, $2, $3, 'node-a-2') RETURNING id",
                    course_id,
                    activity_id,
                    attempt_id,
                )
                assert new_id is not None
            else:
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await schema_conn.execute(
                        "INSERT INTO app.question_opportunities "
                        "(course_id, learning_activity_id, attempt_id, reference_node_id) "
                        "VALUES ($1, $2, $3, 'node-a-3')",
                        course_id,
                        activity_id,
                        attempt_id,
                    )
        finally:
            await tx.rollback()


_OWNER_OR_TEACHER_SELECT_ONLY = [
    ("student_progress__owner_or_teacher_select", "app.student_progress", "user_id"),
    ("learner_state__owner_or_teacher_select", "app.learner_state", "user_id"),
    ("mastery_events__owner_or_teacher_select", "app.mastery_events", "user_id"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("policy", "table", "col"), _OWNER_OR_TEACHER_SELECT_ONLY)
async def test_owner_or_teacher_select_only_policies(schema_conn, policy, table, col):
    """These three tables have a SELECT policy only -- no INSERT/UPDATE
    policy exists for authenticated/app_runtime, so writes from an enforced
    session are unconditionally denied (see the DB-08b report's "open
    concerns" for why this matters for apollo/projections/mastery.py)."""
    for user_id, expected in ((STUDENT_A, 1), (TEACHER_A, 1), (STUDENT_B, 0)):
        tx = schema_conn.transaction()
        await tx.start()
        try:
            await _set_app_runtime(schema_conn, user_id)
            count = await schema_conn.fetchval(f"SELECT count(*) FROM {table} WHERE {col} = $1", STUDENT_A)
            assert count == expected, f"{policy} user={user_id}"
        finally:
            await tx.rollback()


@pytest.mark.asyncio
async def test_mastery_event_insert_denied_under_app_runtime_no_write_policy(schema_conn):
    """CONCRETE regression evidence for the mastery-projection gap (see the
    DB-08b report's "open concerns"): even the OWNING student cannot INSERT
    into app.mastery_events or app.learner_state as app_runtime, because no
    INSERT/UPDATE policy exists on either FORCE-RLS table -- only
    ``*__owner_or_teacher_select``. This is the exact statement
    apollo/projections/mastery.py::update_mastery_from_artifact issues on the
    enforced Done-handler session (apollo/handlers/done.py::_project_mastery),
    which today would start failing (caught, logged, and swallowed -- see
    that function's own docstring) the moment DB-08b enforcement is live for
    a course with APOLLO_GRADING_ARTIFACT_ENABLED on.
    """
    tx = schema_conn.transaction()
    await tx.start()
    try:
        await _set_app_runtime(schema_conn, STUDENT_A)
        course_id = await schema_conn.fetchval("SELECT id FROM app.courses WHERE slug = 'course-one'")
        entity_id = await schema_conn.fetchval(
            "SELECT id FROM app.learner_entities WHERE canonical_key = 'entity-a'"
        )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await schema_conn.execute(
                "INSERT INTO app.mastery_events "
                "(user_id, course_id, entity_id, event_kind, prior_belief, posterior_belief, mastery_after) "
                "VALUES ($1, $2, $3, 'graded', ARRAY[0.34,0.33,0.33]::real[], "
                "ARRAY[0.5,0.3,0.2]::real[], 0.5)",
                STUDENT_A,
                course_id,
                entity_id,
            )
    finally:
        await tx.rollback()

    # learner_state has the same SELECT-only policy shape, but Postgres RLS
    # expresses "no UPDATE policy" differently from "no INSERT policy": with
    # no policy for a command, the command's row-visibility (USING) defaults
    # to false, which for UPDATE just filters the WHERE-matched candidate
    # rows down to zero -- a silent 0-row update, NOT a raised privilege
    # error (INSERT's WITH CHECK(false) default DOES raise, per the block
    # above, because WITH CHECK failures always raise regardless of how many
    # rows matched). Both are denials; only the shape of the denial differs.
    tx = schema_conn.transaction()
    await tx.start()
    try:
        await _set_app_runtime(schema_conn, STUDENT_A)
        result = await schema_conn.execute(
            "UPDATE app.learner_state SET mastery = 0.9 WHERE user_id = $1", STUDENT_A
        )
        assert result == "UPDATE 0"
    finally:
        await tx.rollback()


@pytest.mark.asyncio
async def test_ai_usage_reports_owner_select_and_insert(schema_conn):
    for user_id, expected in ((STUDENT_A, 1), (STUDENT_B, 0)):
        tx = schema_conn.transaction()
        await tx.start()
        try:
            await _set_app_runtime(schema_conn, user_id)
            count = await schema_conn.fetchval(
                "SELECT count(*) FROM app.ai_usage_reports WHERE user_id = $1", STUDENT_A
            )
            assert count == expected
        finally:
            await tx.rollback()

    ids = await _lookup_ids(schema_conn)
    course_id = ids["course_a"]
    for user_id, should_succeed in ((STUDENT_A, True), (STUDENT_B, False)):
        tx = schema_conn.transaction()
        await tx.start()
        try:
            await _set_app_runtime(schema_conn, user_id)
            if should_succeed:
                new_id = await schema_conn.fetchval(
                    "INSERT INTO app.ai_usage_reports (id, user_id, course_id, chat_id, jsonld) "
                    "VALUES (gen_random_uuid(), $1, $2, 'chat-a', '{}'::jsonb) RETURNING id",
                    user_id,
                    course_id,
                )
                assert new_id is not None
            else:
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await schema_conn.execute(
                        "INSERT INTO app.ai_usage_reports (id, user_id, course_id, chat_id, jsonld) "
                        "VALUES (gen_random_uuid(), $1, $2, 'chat-a', '{}'::jsonb)",
                        user_id,
                        course_id,
                    )
        finally:
            await tx.rollback()


@pytest.mark.asyncio
async def test_app_runtime_without_claims_denied_across_representative_tables(schema_conn):
    """app_runtime with NO request.jwt.claims set: auth.uid() reads NULL, so
    every owner/member predicate evaluates false. Probed across one table per
    predicate shape (member_select, owner_all, owner_or_teacher_select,
    teacher_all, self_or_teacher_select) -- the remaining policies share one
    of these five literal predicate mechanisms (verified by inspection of the
    migration SQL), so a regression in any of them would show here too."""
    tx = schema_conn.transaction()
    await tx.start()
    try:
        await _set_app_runtime_no_claims(schema_conn)
        assert await schema_conn.fetchval("SELECT count(*) FROM app.courses") == 0
        assert await schema_conn.fetchval("SELECT count(*) FROM app.learning_activities") == 0
        assert await schema_conn.fetchval("SELECT count(*) FROM app.student_progress") == 0
        assert await schema_conn.fetchval("SELECT count(*) FROM app.course_invites") == 0
        assert await schema_conn.fetchval("SELECT count(*) FROM app.course_memberships") == 0
    finally:
        await tx.rollback()


@pytest.mark.asyncio
async def test_owner_role_bypasses_rls_entirely(schema_conn):
    """The migration-applying/bootstrap role (BYPASSRLS) sees every tenant's
    rows -- proves DB-08b does not touch the owner connection's behavior."""
    count = await schema_conn.fetchval("SELECT count(*) FROM app.courses")
    assert count == 2


# ===========================================================================
# 2. Session-creation-path enforcement boundary (get_db_session enforces;
#    get_async_session never does).
# ===========================================================================


@pytest_asyncio.fixture
async def _fresh_engine_registry(db08b_dsns: tuple[str, str], monkeypatch: pytest.MonkeyPatch):
    """Point database.session's per-loop engine registry at the DB-08b test
    database for the duration of one test, and dispose+clear it afterward.

    Required because _engines is keyed by id(loop), not by DSN -- without
    clearing it, a get_db_session()/get_async_session() call in this test
    file would silently reuse whatever engine an earlier test built for this
    same pytest-asyncio loop, pointed at a DIFFERENT database.
    """
    monkeypatch.setenv("SUPABASE_DB_URL", db08b_dsns[1])
    session_module._engines.clear()
    try:
        yield
    finally:
        for engine, _ in list(session_module._engines.values()):
            await engine.dispose()
        session_module._engines.clear()


async def _drain(agen):
    """Fully exhaust an async generator (mirrors FastAPI's dependency
    teardown for get_db_session)."""
    try:
        await agen.__anext__()
    except StopAsyncIteration:  # pragma: no cover - defensive
        pass


async def _via_get_db_session(user_id: str | None) -> tuple[set[str], str]:
    token = current_request_user_id.set(user_id) if user_id is not None else None
    try:
        agen = get_db_session()
        session = await agen.__anext__()
        try:
            rows = (await session.execute(select(Course))).scalars().all()
            role = (await session.execute(text("SELECT current_user"))).scalar()
        finally:
            await _drain(agen)
    finally:
        if token is not None:
            current_request_user_id.reset(token)
    return {row.slug for row in rows}, role


async def _via_get_async_session() -> tuple[set[str], str]:
    async with get_async_session() as session:
        rows = (await session.execute(select(Course))).scalars().all()
        role = (await session.execute(text("SELECT current_user"))).scalar()
    return {row.slug for row in rows}, role


@pytest.mark.asyncio
async def test_get_db_session_enforces_own_course_only(_fresh_engine_registry):
    slugs, role = await _via_get_db_session(STUDENT_A)
    assert slugs == {"course-one"}
    assert role == "app_runtime"


@pytest.mark.asyncio
async def test_get_db_session_cross_tenant_denied(_fresh_engine_registry):
    slugs, role = await _via_get_db_session(STUDENT_B)
    assert slugs == {"course-two"}
    assert role == "app_runtime"


@pytest.mark.asyncio
async def test_get_db_session_no_identity_stays_owner(_fresh_engine_registry):
    assert current_request_user_id.get() is None
    slugs, role = await _via_get_db_session(None)
    assert slugs == {"course-one", "course-two"}
    assert role != "app_runtime"


@pytest.mark.asyncio
async def test_get_async_session_never_enforces_even_with_identity_set(_fresh_engine_registry):
    """THE regression this test suite exists to catch: a get_async_session()
    session (BackgroundTasks callbacks, ai/router/wiring.py's sync-bridge
    calls, scripts, campaign) must stay on the owner role and see every
    tenant's rows even when current_request_user_id is set in the calling
    context -- exactly the shape of a FastAPI BackgroundTasks callback
    continuing the same Task/context the request handler set the contextvar
    in. If get_db_session's enforcement were wired at the engine level
    instead of per-session-instance, this test would fail (app_runtime,
    single-tenant view) instead of passing.
    """
    token = current_request_user_id.set(STUDENT_A)
    try:
        slugs, role = await _via_get_async_session()
    finally:
        current_request_user_id.reset(token)

    assert slugs == {"course-one", "course-two"}
    assert role != "app_runtime"


@pytest.mark.asyncio
async def test_role_reverts_after_transaction_on_a_reused_connection(db08b_dsns):
    """SET LOCAL scoping: a pooled connection must not stay on app_runtime.

    Forces literal connection reuse with a pool_size=1 engine: the first
    session (via get_db_session, with an identity) claims the sole
    connection and returns it to the pool on close; the second session
    (via get_async_session, no enforcement at all) must get that same
    connection back on the owning role, not still-app_runtime from the first.
    """
    _, sqlalchemy_dsn = db08b_dsns
    engine = create_async_engine(sqlalchemy_dsn, pool_size=1, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    from database.session import _install_rls_context_for_session

    async def _enforced_probe(user_id: str) -> str:
        token = current_request_user_id.set(user_id)
        try:
            async with factory() as session:
                _install_rls_context_for_session(session, engine)
                role = (await session.execute(text("SELECT current_user"))).scalar()
        finally:
            current_request_user_id.reset(token)
        return role

    async def _owner_probe() -> str:
        async with factory() as session:
            # No _install_rls_context_for_session call -- mirrors
            # get_async_session exactly.
            role = (await session.execute(text("SELECT current_user"))).scalar()
        return role

    try:
        role_with_identity = await _enforced_probe(STUDENT_A)
        assert role_with_identity == "app_runtime"

        role_without_identity = await _owner_probe()
        assert role_without_identity != "app_runtime"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_pooled_connections_never_leak_identity_under_concurrent_load(db08b_dsns):
    """4+ concurrent enforced requests across a small pool never see each
    other's rows/role."""
    _, sqlalchemy_dsn = db08b_dsns
    engine = create_async_engine(sqlalchemy_dsn, pool_size=2, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    from database.session import _install_rls_context_for_session

    async def _request(user_id: str, expected_slug: str) -> None:
        token = current_request_user_id.set(user_id)
        try:
            async with factory() as session:
                _install_rls_context_for_session(session, engine)
                rows = (await session.execute(select(Course))).scalars().all()
                role = (await session.execute(text("SELECT current_user"))).scalar()
        finally:
            current_request_user_id.reset(token)
        assert {row.slug for row in rows} == {expected_slug}
        assert role == "app_runtime"

    try:
        requests = []
        for i in range(8):
            if i % 2 == 0:
                requests.append(_request(STUDENT_A, "course-one"))
            else:
                requests.append(_request(STUDENT_B, "course-two"))
        await asyncio.gather(*requests)
    finally:
        await engine.dispose()


# ===========================================================================
# 3. Internal-schema request-path grants (exactly what DB-08b's migration
#    adds -- one probe per granted table/verb).
# ===========================================================================


@pytest.mark.asyncio
async def test_retrieval_rpcs_executable_under_app_runtime(schema_conn):
    """The three sanctioned retrieval RPCs (SECURITY INVOKER, reading only
    internal.document_chunks) run under app_runtime -- proves the EXECUTE +
    SELECT-on-document_chunks grants are both present and sufficient."""
    tx = schema_conn.transaction()
    await tx.start()
    try:
        await _set_app_runtime(schema_conn, STUDENT_A)
        doc_id = await schema_conn.fetchval("SELECT id FROM app.documents WHERE title = 'doc-a'")
        count = await schema_conn.fetchval(
            "SELECT internal.fts_count($1, ARRAY[$2]::integer[])", "chunk", doc_id
        )
        assert count is not None
        rows = await schema_conn.fetch(
            "SELECT * FROM internal.fetch_items(ARRAY[]::integer[], ARRAY[$1]::integer[])", doc_id
        )
        assert rows is not None
    finally:
        await tx.rollback()


@pytest.mark.asyncio
async def test_grading_runs_select_and_insert_under_app_runtime(schema_conn):
    tx = schema_conn.transaction()
    await tx.start()
    try:
        await _set_app_runtime(schema_conn, STUDENT_A)
        count = await schema_conn.fetchval(
            "SELECT count(*) FROM internal.grading_runs WHERE user_id = $1", STUDENT_A
        )
        assert count == 1
        course_id = await schema_conn.fetchval("SELECT id FROM app.courses WHERE slug = 'course-one'")
        attempt_id = await schema_conn.fetchval(
            "SELECT id FROM app.problem_attempts WHERE user_id = $1", STUDENT_A
        )
        problem_id = await schema_conn.fetchval(
            "SELECT id FROM app.problems WHERE problem_code = 'problem-a'"
        )
        # grader_version differs from the seeded row's 'v1' -- the seed row
        # already occupies (attempt_id, role='canonical', grader_version='v1')
        # under grading_runs' unique constraint, so this proves a fresh
        # INSERT succeeds without colliding with seed data.
        new_id = await schema_conn.fetchval(
            "INSERT INTO internal.grading_runs "
            "(course_id, user_id, attempt_id, problem_id, role, grader_used, grader_version) "
            "VALUES ($1, $2, $3, $4, 'canonical', 'llm_fallback', 'v2') RETURNING id",
            course_id,
            STUDENT_A,
            attempt_id,
            problem_id,
        )
        assert new_id is not None
    finally:
        await tx.rollback()


@pytest.mark.asyncio
async def test_entity_prerequisites_select_and_insert_under_app_runtime(schema_conn):
    tx = schema_conn.transaction()
    await tx.start()
    try:
        await _set_app_runtime(schema_conn, STUDENT_A)
        count = await schema_conn.fetchval("SELECT count(*) FROM internal.entity_prerequisites")
        assert count == 0  # none seeded yet -- proves SELECT works (not an error), not that rows exist
        course_id = await schema_conn.fetchval("SELECT id FROM app.courses WHERE slug = 'course-one'")
        e1 = await schema_conn.fetchval(
            "SELECT id FROM app.learner_entities WHERE canonical_key = 'entity-a'"
        )
        e2 = await schema_conn.fetchval(
            "SELECT id FROM app.learner_entities WHERE canonical_key = 'entity-a2'"
        )
        await schema_conn.execute(
            "INSERT INTO internal.entity_prerequisites (course_id, from_entity_id, to_entity_id) "
            "VALUES ($1, $2, $3)",
            course_id,
            e1,
            e2,
        )
        count_after = await schema_conn.fetchval("SELECT count(*) FROM internal.entity_prerequisites")
        assert count_after == 1
    finally:
        await tx.rollback()


@pytest.mark.asyncio
async def test_content_ingest_runs_select_only_under_app_runtime(schema_conn):
    tx = schema_conn.transaction()
    await tx.start()
    try:
        await _set_app_runtime(schema_conn, STUDENT_A)
        count = await schema_conn.fetchval("SELECT count(*) FROM internal.content_ingest_runs")
        assert count == 2  # one per seeded tenant
        course_id = await schema_conn.fetchval("SELECT id FROM app.courses WHERE slug = 'course-one'")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await schema_conn.execute(
                "INSERT INTO internal.content_ingest_runs (course_id) VALUES ($1)", course_id
            )
    finally:
        await tx.rollback()


@pytest.mark.asyncio
async def test_dedup_decisions_insert_and_delete_under_app_runtime(schema_conn):
    """SELECT is granted alongside INSERT/DELETE -- not because application
    code reads this table back, but because dedup_decisions.id is an IDENTITY
    column and SQLAlchemy's asyncpg dialect defaults to
    ``INSERT ... RETURNING id`` (implicit_returning=True), which requires
    SELECT on the returned column. This test proves both the RETURNING-shaped
    INSERT and an explicit read-back succeed, and DELETE works."""
    tx = schema_conn.transaction()
    await tx.start()
    try:
        await _set_app_runtime(schema_conn, STUDENT_A)
        course_id = await schema_conn.fetchval("SELECT id FROM app.courses WHERE slug = 'course-one'")
        new_id = await schema_conn.fetchval(
            "INSERT INTO internal.dedup_decisions (course_id, candidate_key, method, verdict) "
            "VALUES ($1, 'k', 'slug', 'distinct') RETURNING id",
            course_id,
        )
        assert new_id is not None
        count = await schema_conn.fetchval(
            "SELECT count(*) FROM internal.dedup_decisions WHERE id = $1", new_id
        )
        assert count == 1
        deleted = await schema_conn.fetchval(
            "DELETE FROM internal.dedup_decisions WHERE id = $1 RETURNING id", new_id
        )
        assert deleted is not None
    finally:
        await tx.rollback()


@pytest.mark.asyncio
async def test_ingest_page_evidence_select_only_under_app_runtime(schema_conn):
    tx = schema_conn.transaction()
    await tx.start()
    try:
        await _set_app_runtime(schema_conn, STUDENT_A)
        count = await schema_conn.fetchval("SELECT count(*) FROM internal.ingest_page_evidence")
        assert count == 2
        run_id = await schema_conn.fetchval("SELECT id FROM internal.content_ingest_runs LIMIT 1")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await schema_conn.execute(
                "INSERT INTO internal.ingest_page_evidence "
                "(course_id, ingest_run_id, role, page_number, ocr_text) "
                "VALUES ((SELECT id FROM app.courses LIMIT 1), $1, 'problem', 1, 'x')",
                run_id,
            )
    finally:
        await tx.rollback()


@pytest.mark.asyncio
async def test_ungranted_internal_tables_denied_under_app_runtime(schema_conn):
    """upload_jobs (worker-only), chat_session_snippets / chat_routing_decisions
    (sync-bridge only), content_ingest_errors (BackgroundTasks-only, never read
    back) -- none of these get a grant, so app_runtime is denied on all of
    them, exactly like it was before this migration."""
    for table in (
        "internal.upload_jobs",
        "internal.chat_session_snippets",
        "internal.chat_routing_decisions",
        "internal.content_ingest_errors",
    ):
        # One transaction per table: a permission-denied error aborts the
        # surrounding transaction (all subsequent statements in it raise
        # InFailedSQLTransactionError, not the specific error being tested),
        # so each probe needs its own transaction to stay independently
        # meaningful.
        tx = schema_conn.transaction()
        await tx.start()
        try:
            await _set_app_runtime(schema_conn, STUDENT_A)
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await schema_conn.fetchval(f"SELECT count(*) FROM {table}")
        finally:
            await tx.rollback()


@pytest.mark.asyncio
async def test_document_chunks_select_only_no_write_under_app_runtime(schema_conn):
    tx = schema_conn.transaction()
    await tx.start()
    try:
        await _set_app_runtime(schema_conn, STUDENT_A)
        count = await schema_conn.fetchval("SELECT count(*) FROM internal.document_chunks")
        assert count == 2
        doc_id = await schema_conn.fetchval("SELECT id FROM app.documents WHERE title = 'doc-a'")
        course_id = await schema_conn.fetchval("SELECT id FROM app.courses WHERE slug = 'course-one'")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await schema_conn.execute(
                "INSERT INTO internal.document_chunks (course_id, document_id, content) "
                "VALUES ($1, $2, 'denied')",
                course_id,
                doc_id,
            )
    finally:
        await tx.rollback()


# ===========================================================================
# 4. Route-level integration through the REAL FastAPI dependency graph.
#
# The security review's HIGH finding: every test above proves the SET-LOCAL-
# ROLE mechanism works GIVEN a correctly-ordered contextvar, but none of them
# drive an actual ASGI request through `get_db_session` + `require_user` /
# `require_session_owner` the way FastAPI's dependency resolver actually
# does -- `_via_get_db_session` above sets `current_request_user_id` BEFORE
# calling `get_db_session()`, an ordering the real request path never
# produces (`get_db_session` is a `Depends` parameter FastAPI resolves on its
# own schedule, not something route code calls directly). This section
# closes that gap with TestClient-driven requests through real ASGI routes,
# using the REAL `require_user` / `require_session_owner` / `get_db_session`
# production callables -- only `resolve_auth_context` is faked (no network
# call to Supabase/GoTrue).
#
# The first two routes mirror the two dependency SHAPES every real
# /apollo/* route uses (see apollo/api.py): `body_call_probe` mirrors
# create_session/progress/concepts/problems/classroom_* (db: Depends
# parameter, require_user(request) called explicitly in the body);
# `session_owner_probe` mirrors get_session/chat/done/retry/next/end/kg/*
# (db: Depends declared BEFORE auth: Depends(require_session_owner) --
# exactly the ordering the CRITICAL finding's own repro used to prove
# enforcement was inert). The last block hits the REAL, unmodified
# apollo.api.router (including the problem_generation sub-router) to prove
# the query-before-auth ordering bug fixed in
# apollo/provisioning/problem_generation/api.py stays fixed end to end.
# ===========================================================================


def _fake_resolve_auth_context(request: Request) -> AuthContext:
    """Sync stand-in for auth.resolve_auth_context (require_user awaits it
    via asyncio.to_thread, so this must stay a plain sync callable, matching
    the real function's signature). No network call: the test identity comes
    from a header the real Supabase-backed function has no equivalent of."""
    user_id = request.headers.get("X-Test-User")
    if not user_id:
        raise HTTPException(status_code=401, detail="missing X-Test-User header")
    return AuthContext(user_id=user_id, access_token="test-token")


def _build_diagnostic_app() -> FastAPI:
    app = FastAPI()
    router = APIRouter()

    @router.get("/probe/body-call")
    async def body_call_probe(
        request: Request,
        db: AsyncSession = Depends(get_db_session),
    ) -> dict:
        auth = await require_user(request)
        role = (await db.execute(text("SELECT current_user"))).scalar()
        slugs = (await db.execute(select(Course.slug))).scalars().all()
        return {"user_id": auth.user_id, "role": role, "slugs": sorted(slugs)}

    @router.get("/probe/session-owner/{session_id}")
    async def session_owner_probe(
        session_id: int,
        db: AsyncSession = Depends(get_db_session),
        auth: AuthContext = Depends(require_session_owner),
    ) -> dict:
        role = (await db.execute(text("SELECT current_user"))).scalar()
        return {"user_id": auth.user_id, "role": role}

    app.include_router(router)
    return app


@pytest.fixture
def _testclient_db_env(db08b_dsns: tuple[str, str], monkeypatch: pytest.MonkeyPatch):
    """Point SUPABASE_DB_URL at the DB-08b test database for a TestClient-
    driven test -- WITHOUT ``_fresh_engine_registry``'s dispose-everything
    teardown loop.

    Deliberately NOT reusing ``_fresh_engine_registry`` here: TestClient (the
    sync interface) runs the ASGI app through its own background portal
    thread with its OWN event loop, so ``get_db_session`` mints an engine
    keyed to THAT loop's id, not this fixture's (or the test's) outer
    pytest-asyncio loop. ``_fresh_engine_registry``'s teardown does
    ``await engine.dispose()`` on every engine in the process-wide registry
    from the OUTER loop -- awaiting/disposing an asyncpg engine from a
    different loop than the one its connections were opened on is unsafe
    (asyncio Future/Task objects are loop-bound), and by the time that
    teardown runs, TestClient's `__exit__` (which fires first -- fixture
    teardown is LIFO, and the client fixture that depends on this one tears
    down before this one) has already closed the portal loop the engine
    belongs to. Empirically, doing so crashed the test process with
    `Windows fatal exception: access violation` inside asyncpg's C
    extension (non-fatal to the overall pytest run here, but unsafe and not
    something to rely on). The env var alone is sufficient for correctness --
    ``db08b_dsns`` is session-scoped so every test in this file already
    shares the same DSN, and any pre-existing registry entry for the OUTER
    loop (if the test also uses `schema_conn`/raw asyncpg, which never goes
    through `_engines`) is unaffected. The TestClient-minted engine/pool is
    simply left to the OS/GC once the portal thread exits -- an accepted
    trade-off for a short-lived test process, not something to do in
    request-serving code.
    """
    monkeypatch.setenv("SUPABASE_DB_URL", db08b_dsns[1])


@pytest.fixture
def _diagnostic_client(_testclient_db_env, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(auth_deps_module, "resolve_auth_context", _fake_resolve_auth_context)
    app = _build_diagnostic_app()
    with TestClient(app) as tc:
        yield tc


@pytest.fixture
def _real_apollo_client(_testclient_db_env, monkeypatch: pytest.MonkeyPatch):
    """TestClient wired onto the real, unmodified apollo.api.router (the
    actual production route tree, including the problem_generation
    sub-router) -- for proving the query-before-auth fix end to end, through
    the real route path strings."""
    from apollo.api import router as real_apollo_router

    monkeypatch.setattr(auth_deps_module, "resolve_auth_context", _fake_resolve_auth_context)
    app = FastAPI()
    app.include_router(real_apollo_router)
    with TestClient(app) as tc:
        yield tc


def test_route_level_body_call_shape_enforces_app_runtime_and_own_tenant(_diagnostic_client):
    """THE CRITICAL-finding regression test for the body-call shape
    (create_session/progress/concepts/problems/classroom_*): before the
    DB-08b ordering fix, `_install_rls_context_for_session` read
    `current_request_user_id` eagerly at session-mint time -- None, because
    `require_user` (which sets it) had not run yet -- so this would have
    observed the owner (BYPASSRLS) role and every tenant's course slug."""
    resp = _diagnostic_client.get("/probe/body-call", headers={"X-Test-User": STUDENT_A})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["role"] == "app_runtime"
    assert body["slugs"] == ["course-one"]


def test_route_level_body_call_shape_cross_tenant_scoped(_diagnostic_client):
    resp = _diagnostic_client.get("/probe/body-call", headers={"X-Test-User": STUDENT_B})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["role"] == "app_runtime"
    assert body["slugs"] == ["course-two"]


def test_route_level_body_call_shape_no_identity_401s_before_any_query(_diagnostic_client):
    resp = _diagnostic_client.get("/probe/body-call")
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_route_level_session_owner_shape_enforces_app_runtime(_diagnostic_client, schema_conn):
    """THE CRITICAL-finding regression test for the require_session_owner
    shape (get_session/chat/done/retry/next/end/kg/*): `session_owner_probe`
    declares `db: Depends(get_db_session)` BEFORE
    `auth: Depends(require_session_owner)`, exactly like the real routes --
    the precise ordering the finding's own TestClient repro used to prove
    enforcement was inert. Session-object mint order no longer matters (see
    database/session.py's lazy read); what matters is that
    require_session_owner resolves identity before its own first query on
    `db`, which it already did both before and after this fix."""
    ids = await _lookup_ids(schema_conn)
    resp = _diagnostic_client.get(
        f"/probe/session-owner/{ids['tutoring_a']}", headers={"X-Test-User": STUDENT_A}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "app_runtime"


@pytest.mark.asyncio
async def test_route_level_session_owner_shape_denies_non_owner(_diagnostic_client, schema_conn):
    ids = await _lookup_ids(schema_conn)
    resp = _diagnostic_client.get(
        f"/probe/session-owner/{ids['tutoring_a']}", headers={"X-Test-User": STUDENT_B}
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_problem_generation_seeds_route_teacher_allowed_end_to_end(
    _real_apollo_client, schema_conn
):
    """Regression test for the query-before-auth ordering bug fixed in
    apollo/provisioning/problem_generation/api.py::list_generation_seeds
    (it used to call `_course_concept_or_404(db, concept_id)` -- a query --
    before `require_user(request)`, permanently locking that request's
    transaction onto the unenforced owner role). Hits the real route path
    end to end."""
    concept_a = await schema_conn.fetchval("SELECT id FROM app.concepts WHERE slug = 'concept-a'")
    resp = _real_apollo_client.get(
        f"/apollo/problem-generation/concepts/{concept_a}/seeds",
        headers={"X-Test-User": TEACHER_A},
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_problem_generation_seeds_route_non_teacher_denied_end_to_end(
    _real_apollo_client, schema_conn
):
    concept_a = await schema_conn.fetchval("SELECT id FROM app.concepts WHERE slug = 'concept-a'")
    resp = _real_apollo_client.get(
        f"/apollo/problem-generation/concepts/{concept_a}/seeds",
        headers={"X-Test-User": STUDENT_A},
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_problem_generation_seeds_route_cross_tenant_denied_end_to_end(
    _real_apollo_client, schema_conn
):
    """STUDENT_B has no membership in course A at all, so this denial is a
    real, unplanned proof of DB-08b enforcement on the fixed route: the FIRST
    query the (now-correctly-ordered) handler issues is
    ``_course_concept_or_404``'s SELECT on ``app.concepts`` -- under
    ``app_runtime`` with STUDENT_B's claims, the ``concepts__member_select``
    RLS policy filters that row out (STUDENT_B is not a member of course A),
    so the handler observes NO ROW and 404s exactly as it would for a
    genuinely-missing concept -- the app-layer ``require_course_teacher``
    check one line later never even runs. This is a STRONGER assertion than
    the 403 an app-layer-only membership check would have produced (which is
    what `test_problem_generation_seeds_route_non_teacher_denied_end_to_end`
    above exercises, for STUDENT_A -- a real course-A member the
    member_select policy does NOT filter, so THAT request reaches and 403s
    on the app-layer teacher check instead)."""
    concept_a = await schema_conn.fetchval("SELECT id FROM app.concepts WHERE slug = 'concept-a'")
    resp = _real_apollo_client.get(
        f"/apollo/problem-generation/concepts/{concept_a}/seeds",
        headers={"X-Test-User": STUDENT_B},
    )
    assert resp.status_code == 404, resp.text
