"""DB-08d: real-Postgres tests for the inline-membership RLS policy rewrite.

DB-08d (20260731120000) rewrites 38 of the 45 app-schema policies so the
membership check is an UNCORRELATED subquery over ``app.course_memberships``
instead of a per-row ``internal.has_course_role()`` call. It must not change
which rows any role can see or write.

tests/database/test_db08b_rls_enforcement.py already applies DB-08d and runs
the entire pre-existing 45-policy own/cross-tenant/no-claims/owner-bypass
matrix against it -- that is the equivalence suite, unchanged assertions and
all. This module covers what that matrix cannot:

1. Structure: the rewrite is in place -- same 45 policy names, same command,
   same TO-role -- and ``has_course_role`` survives in exactly one policy
   (``course_memberships__self_or_teacher_select``, which cannot be inlined
   without recursing on its own table).
2. Re-runnability: applying the migration a second time is a no-op.
3. The three role distinctions ``has_course_role`` encodes, on a fixture the
   DB-08b matrix does not have -- TWO students in the SAME course, so a
   student-vs-teacher confusion in any rewritten expression is observable:
     * a student sees their OWN learner-scoped rows and not a co-student's;
     * a teacher of the course sees BOTH students' rows;
     * a non-member (and a teacher of a different course) sees nothing;
     * a student does NOT inherit teacher-only powers on their own course.
   Every one of these would break loudly if a ``role = 'teacher'`` predicate
   were widened to ``role = ANY(ARRAY['student','teacher'])`` (or vice versa)
   anywhere in the 38 rewritten expressions.
4. Plan shape: the rewritten policy produces a HASHED subplan (built once per
   query) rather than the per-row correlated SubPlan the old form forced --
   the entire point of the migration, and a silent-regression risk if someone
   later re-correlates one of these expressions.
"""

from __future__ import annotations

import asyncio
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
_RETRIEVAL = _MIGRATIONS / "20260717050000_retrieval_functions_v1.sql"
_GRANTS = _MIGRATIONS / "20260722120000_db08b_rls_enforcement_grants.sql"
_POLICY_GAPS = _MIGRATIONS / "20260723060000_db08c_rls_write_policy_gaps.sql"
_GROUNDING_BUNDLE = _MIGRATIONS / "20260728120000_apollo_grounding_bundle.sql"
_INLINE_POLICIES = _MIGRATIONS / "20260731120000_db08d_rls_inline_membership_policies.sql"
_DB_NAME = "db08d_inline_membership_policies"

# Course A: one teacher, TWO students. Course B: one teacher, one student.
# Plus an enrolled-nowhere user. The two co-students in course A are what make
# the student-vs-teacher distinction observable.
STUDENT_A1 = "a1000000-0000-4000-8000-000000000001"
STUDENT_A2 = "a2000000-0000-4000-8000-000000000002"
TEACHER_A = "a3000000-0000-4000-8000-000000000003"
STUDENT_B1 = "b1000000-0000-4000-8000-000000000004"
TEACHER_B = "b2000000-0000-4000-8000-000000000005"
OUTSIDER = "c0000000-0000-4000-8000-000000000006"

# The one policy that must still call the SECURITY DEFINER helper: inlining a
# membership subquery into a policy ON app.course_memberships would raise
# "infinite recursion detected in policy for relation course_memberships".
_EXPECTED_REMAINING_HELPER_CALLER = "course_memberships__self_or_teacher_select"

# The 7 policies that never referenced has_course_role and must come through
# the rewrite byte-identical.
_UNTOUCHED_POLICIES = {
    ("ai_usage_reports", "ai_usage_reports__owner_select"),
    ("course_memberships", "course_memberships__self_insert"),
    ("course_memberships", "course_memberships__self_or_teacher_select"),
    ("learner_state", "learner_state__owner_update"),
    ("question_opportunities", "question_opportunities__owner_insert"),
    ("question_opportunities", "question_opportunities__owner_update"),
    ("tutoring_messages", "tutoring_messages__owner_delete"),
}

# Same Supabase Auth role/function shim the rest of the real-Postgres migration
# tests use (see tests/database/test_db08b_rls_enforcement.py for why auth.uid()
# must nullif BEFORE casting to json).
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
  SELECT (nullif(current_setting('request.jwt.claims', true), '')::json->>'sub')::uuid
$$;
GRANT USAGE ON SCHEMA auth TO anon, authenticated;
GRANT EXECUTE ON FUNCTION auth.uid() TO anon, authenticated;
"""

_POLICY_QUERY = """
SELECT c.relname AS tbl,
       p.polname AS polname,
       p.polcmd::text AS polcmd,
       (SELECT string_agg(r.rolname, ',' ORDER BY r.rolname)
          FROM pg_roles AS r WHERE r.oid = ANY (p.polroles)) AS roles,
       pg_get_expr(p.polqual, p.polrelid) AS using_expr,
       pg_get_expr(p.polwithcheck, p.polrelid) AS check_expr
FROM pg_policy AS p
JOIN pg_class AS c ON c.oid = p.polrelid
JOIN pg_namespace AS n ON n.oid = c.relnamespace
WHERE n.nspname = 'app'
ORDER BY c.relname, p.polname
"""


def _dsn(url: str, database: str) -> str:
    return (
        make_url(url)
        .set(drivername="postgresql", database=database)
        .render_as_string(hide_password=False)
    )


async def _seed(conn: asyncpg.Connection) -> None:
    """Two tenants over an admin (BYPASSRLS) connection, so RLS never
    interferes with seeding. Course A carries two students; every
    learner-scoped table gets one row per student so ownership and
    teacher-visibility are separable."""
    await conn.executemany(
        "INSERT INTO auth.users (id) VALUES ($1)",
        [
            (STUDENT_A1,),
            (STUDENT_A2,),
            (TEACHER_A,),
            (STUDENT_B1,),
            (TEACHER_B,),
            (OUTSIDER,),
        ],
    )
    course_a = await conn.fetchval(
        "INSERT INTO app.courses (name, slug, subject_name) "
        "VALUES ('Course A', 'course-a', 'Physics') RETURNING id"
    )
    course_b = await conn.fetchval(
        "INSERT INTO app.courses (name, slug, subject_name) "
        "VALUES ('Course B', 'course-b', 'Math') RETURNING id"
    )
    await conn.executemany(
        "INSERT INTO app.course_memberships (user_id, course_id, role) VALUES ($1, $2, $3)",
        [
            (STUDENT_A1, course_a, "student"),
            (STUDENT_A2, course_a, "student"),
            (TEACHER_A, course_a, "teacher"),
            (STUDENT_B1, course_b, "student"),
            (TEACHER_B, course_b, "teacher"),
        ],
    )

    for tag, course_id, learners in (
        ("a", course_a, (STUDENT_A1, STUDENT_A2)),
        ("b", course_b, (STUDENT_B1,)),
    ):
        concept_id = await conn.fetchval(
            "INSERT INTO app.concepts "
            "(course_id, subject_slug, subject_display_name, slug, display_name) "
            "VALUES ($1, 'physics', 'Physics', $2, $2) RETURNING id",
            course_id,
            f"concept-{tag}",
        )
        await conn.execute(
            "INSERT INTO app.documents (course_id, title, content, content_hash) "
            "VALUES ($1, $2, 'body', $3)",
            course_id,
            f"doc-{tag}",
            f"hash-{tag}",
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
        await conn.execute(
            "INSERT INTO app.uploads (course_id, week, kind, title) VALUES ($1, 1, 'notes', $2)",
            course_id,
            f"upload-{tag}",
        )
        await conn.execute(
            "INSERT INTO app.provisioning_runs (course_id, kind, set_index) "
            "VALUES ($1, 'authored_set', 1)",
            course_id,
        )
        for index, learner in enumerate(learners):
            await conn.execute(
                "INSERT INTO app.course_invites (code, course_id, role, created_by) "
                "VALUES ($1, $2, 'student', $3)",
                f"invite-{tag}-{index}",
                course_id,
                TEACHER_A if tag == "a" else TEACHER_B,
            )
            chat_id = await conn.fetchval(
                "INSERT INTO app.learning_activities "
                "(modality, external_id, user_id, course_id, status) "
                "VALUES ('chat', $1, $2, $3, 'active') RETURNING id",
                f"chat-{tag}-{index}",
                learner,
                course_id,
            )
            tutoring_id = await conn.fetchval(
                "INSERT INTO app.learning_activities "
                "(modality, user_id, course_id, status, phase) "
                "VALUES ('tutoring', $1, $2, 'active', 'TEACHING') RETURNING id",
                learner,
                course_id,
            )
            await conn.execute(
                "INSERT INTO app.chat_messages "
                "(course_id, learning_activity_id, turn_index, external_id, role, content) "
                "VALUES ($1, $2, 0, $3, 'user', 'hi')",
                course_id,
                chat_id,
                f"chat-msg-{tag}-{index}",
            )
            attempt_id = await conn.fetchval(
                "INSERT INTO app.problem_attempts "
                "(course_id, user_id, learning_activity_id, problem_id, difficulty) "
                "VALUES ($1, $2, $3, $4, 'standard') RETURNING id",
                course_id,
                learner,
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
                f"node-{tag}-{index}",
            )
            await conn.execute(
                "INSERT INTO app.student_progress (user_id, course_id) VALUES ($1, $2)",
                learner,
                course_id,
            )
            await conn.execute(
                "INSERT INTO app.learner_state "
                "(user_id, course_id, entity_id, belief, mastery, confidence) "
                "VALUES ($1, $2, $3, ARRAY[0.34,0.33,0.33]::real[], 0.5, 0.5)",
                learner,
                course_id,
                entity_id,
            )
            await conn.execute(
                "INSERT INTO app.mastery_events "
                "(user_id, course_id, entity_id, attempt_id, event_kind, "
                " prior_belief, posterior_belief, mastery_after) "
                "VALUES ($1, $2, $3, $4, 'graded', "
                " ARRAY[0.34,0.33,0.33]::real[], ARRAY[0.5,0.3,0.2]::real[], 0.5)",
                learner,
                course_id,
                entity_id,
                attempt_id,
            )
            await conn.execute(
                "INSERT INTO app.ai_usage_reports (id, user_id, course_id, chat_id, jsonld) "
                "VALUES (gen_random_uuid(), $1, $2, $3, '{}'::jsonb)",
                learner,
                course_id,
                f"chat-{tag}-{index}",
            )


@pytest.fixture(scope="session")
def db08d_dsn(request: pytest.FixtureRequest) -> str:
    """Fresh DB with the full chain through DB-08d applied, plus seed data."""
    pg_url = request.getfixturevalue("_pg_url")
    base_database = make_url(pg_url).database
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
            for migration in (
                _SNAPSHOT,
                _CREATE,
                _RETRIEVAL,
                _GRANTS,
                _POLICY_GAPS,
                _GROUNDING_BUNDLE,
                _INLINE_POLICIES,
            ):
                await conn.execute(migration.read_text(encoding="utf-8"))
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
        yield test_dsn
    finally:
        asyncio.run(teardown())


@pytest_asyncio.fixture
async def conn(db08d_dsn: str):
    connection = await asyncpg.connect(db08d_dsn)
    try:
        yield connection
    finally:
        await connection.close()


async def _as(connection: asyncpg.Connection, user_id: str | None) -> None:
    """Reproduce database/session.py's enforcement handshake exactly."""
    await connection.execute("SET LOCAL ROLE app_runtime")
    if user_id is not None:
        await connection.execute(
            "SELECT set_config('request.jwt.claims', $1, true)",
            f'{{"sub":"{user_id}","role":"authenticated"}}',
        )


async def _count_as(connection: asyncpg.Connection, user_id: str | None, sql: str, *args) -> int:
    tx = connection.transaction()
    await tx.start()
    try:
        await _as(connection, user_id)
        return await connection.fetchval(sql, *args)
    finally:
        await tx.rollback()


# ===========================================================================
# 1. Structure of the rewrite.
# ===========================================================================


@pytest.mark.asyncio
async def test_policy_inventory_is_unchanged_by_the_rewrite(conn):
    """DB-08d rewrites expressions IN PLACE: no policy added, dropped, or
    re-scoped. Any drift here means the rewrite changed the policy surface,
    not just how each expression is evaluated."""
    rows = await conn.fetch(_POLICY_QUERY)
    assert len(rows) == 45
    # Every policy is still authenticated-scoped -- app_runtime binds through
    # its membership in that role (DB-08b), nothing binds directly.
    assert {row["roles"] for row in rows} == {"authenticated"}
    # Command mix: 15 SELECT (r), 11 INSERT (a), 10 UPDATE (w), 5 DELETE (d),
    # 4 ALL (*) -- exactly the DB-04 + DB-08c baseline.
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["polcmd"]] = counts.get(row["polcmd"], 0) + 1
    assert counts == {"r": 15, "a": 11, "w": 10, "d": 5, "*": 4}


@pytest.mark.asyncio
async def test_has_course_role_survives_only_where_inlining_would_recurse(conn):
    """37 of the 38 rewritten policies dropped the helper outright; the only
    remaining caller is the policy ON app.course_memberships, whose inline
    form would recurse on its own table."""
    rows = await conn.fetch(_POLICY_QUERY)
    callers = sorted(
        row["polname"]
        for row in rows
        if "has_course_role" in ((row["using_expr"] or "") + (row["check_expr"] or ""))
    )
    assert callers == [_EXPECTED_REMAINING_HELPER_CALLER]

    # The helper itself is untouched -- still present, still SECURITY DEFINER.
    is_security_definer = await conn.fetchval(
        "SELECT prosecdef FROM pg_proc WHERE oid = "
        "to_regprocedure('internal.has_course_role(bigint, text[])')"
    )
    assert is_security_definer is True


@pytest.mark.asyncio
async def test_every_rewritten_policy_reads_course_memberships_inline(conn):
    """The 38 rewritten policies each carry an inline
    ``app.course_memberships`` subquery; the 7 that never had a membership
    check carry none."""
    rows = await conn.fetch(_POLICY_QUERY)
    inlined, plain = set(), set()
    for row in rows:
        key = (row["tbl"], row["polname"])
        expr = (row["using_expr"] or "") + (row["check_expr"] or "")
        (inlined if "app.course_memberships cm" in expr else plain).add(key)
    assert plain == _UNTOUCHED_POLICIES
    assert len(inlined) == 38


@pytest.mark.asyncio
async def test_inline_subquery_reuses_the_same_current_user_expression(conn):
    """The rewrite must resolve the caller the SAME way the old policies did --
    ``(SELECT auth.uid())``, i.e. the ``request.jwt.claims`` -> ``sub`` value
    database/session.py binds with ``set_config(..., true)``. A different
    expression (current_user, a new GUC) would silently change who each policy
    is about."""
    rows = await conn.fetch(_POLICY_QUERY)
    for row in rows:
        expr = (row["using_expr"] or "") + (row["check_expr"] or "")
        if "app.course_memberships cm" not in expr:
            continue
        assert "cm.user_id = ( SELECT auth.uid() AS uid)" in expr, row["polname"]


@pytest.mark.asyncio
async def test_migration_is_rerunnable(conn):
    """DROP POLICY IF EXISTS + CREATE POLICY: a second apply is a no-op, both
    in count and in every deparsed expression."""
    before = {(r["tbl"], r["polname"]): dict(r) for r in await conn.fetch(_POLICY_QUERY)}
    # Applied WITHOUT a wrapping transaction on purpose: the migration file
    # carries its own BEGIN/COMMIT, and nesting it inside an asyncpg
    # transaction would make its COMMIT close the outer one. Idempotence is
    # exactly what this test asserts, so leaving the re-apply committed is safe.
    await conn.execute(_INLINE_POLICIES.read_text(encoding="utf-8"))
    after = {(r["tbl"], r["polname"]): dict(r) for r in await conn.fetch(_POLICY_QUERY)}
    assert after == before


# ===========================================================================
# 2. The role distinctions has_course_role encoded. Course A has TWO students,
#    so student-vs-teacher confusion in any rewritten expression is visible.
# ===========================================================================

# (table, id column) for the learner-scoped tables whose SELECT policy is
# "own row OR any row in a course you teach".
_OWNER_OR_TEACHER_TABLES = [
    ("app.student_progress", "user_id"),
    ("app.learner_state", "user_id"),
    ("app.mastery_events", "user_id"),
    ("app.problem_attempts", "user_id"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("table", "owner_col"), _OWNER_OR_TEACHER_TABLES)
async def test_student_sees_only_own_rows_not_a_co_students(conn, table, owner_col):
    """STUDENT_A1 and STUDENT_A2 are BOTH members of course A, so a rewrite
    that widened the teacher arm to any member would show each of them the
    other's rows. It must not."""
    own = await _count_as(
        conn, STUDENT_A1, f"SELECT count(*) FROM {table} WHERE {owner_col} = $1", STUDENT_A1
    )
    assert own >= 1
    co_student = await _count_as(
        conn, STUDENT_A1, f"SELECT count(*) FROM {table} WHERE {owner_col} = $1", STUDENT_A2
    )
    assert co_student == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(("table", "owner_col"), _OWNER_OR_TEACHER_TABLES)
async def test_teacher_sees_every_students_rows_in_their_own_course(conn, table, owner_col):
    """The teacher arm must still resolve -- the course teacher sees BOTH
    course-A students. A rewrite that narrowed it to the owner would show the
    teacher (who owns no learner rows) nothing at all."""
    for learner in (STUDENT_A1, STUDENT_A2):
        seen = await _count_as(
            conn, TEACHER_A, f"SELECT count(*) FROM {table} WHERE {owner_col} = $1", learner
        )
        assert seen >= 1, learner


@pytest.mark.asyncio
@pytest.mark.parametrize(("table", "owner_col"), _OWNER_OR_TEACHER_TABLES)
async def test_other_course_teacher_and_non_member_see_nothing(conn, table, owner_col):
    """Membership is per-course: teaching course B grants nothing in course A,
    and an enrolled-nowhere user sees no rows at all."""
    for actor, expected in ((TEACHER_B, 1), (OUTSIDER, 0)):
        # TEACHER_B sees exactly course B's single learner row, never course A's.
        seen = await _count_as(conn, actor, f"SELECT count(*) FROM {table}")
        assert seen == expected, actor
        in_course_a = await _count_as(
            conn, actor, f"SELECT count(*) FROM {table} WHERE {owner_col} = $1", STUDENT_A1
        )
        assert in_course_a == 0, actor


# (table, filter column, course-A value) for the "any member of the course may
# read it" curriculum tables.
_MEMBER_SELECT_TABLES = [
    ("app.courses", "slug", "course-a"),
    ("app.documents", "title", "doc-a"),
    ("app.concepts", "slug", "concept-a"),
    ("app.problems", "problem_code", "problem-a"),
    ("app.learner_entities", "canonical_key", "entity-a"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("table", "col", "value"), _MEMBER_SELECT_TABLES)
async def test_member_select_covers_students_and_teacher_but_not_outsiders(conn, table, col, value):
    """Both roles of the course see the course's curriculum row; the other
    course's members and the non-member do not."""
    for actor, expected in (
        (STUDENT_A1, 1),
        (STUDENT_A2, 1),
        (TEACHER_A, 1),
        (STUDENT_B1, 0),
        (TEACHER_B, 0),
        (OUTSIDER, 0),
    ):
        seen = await _count_as(conn, actor, f"SELECT count(*) FROM {table} WHERE {col} = $1", value)
        assert seen == expected, (actor, table)


# Teacher-only FOR ALL tables: no student of the course may see them.
_TEACHER_ONLY_TABLES = ["app.uploads", "app.course_invites", "app.provisioning_runs"]


@pytest.mark.asyncio
@pytest.mark.parametrize("table", _TEACHER_ONLY_TABLES)
async def test_teacher_only_tables_stay_invisible_to_students(conn, table):
    """The sharpest equivalence check on the teacher predicate: if any
    ``cm.role = 'teacher'`` had been widened to the member array, an enrolled
    student would start seeing their course's invites and uploads."""
    assert await _count_as(conn, TEACHER_A, f"SELECT count(*) FROM {table}") >= 1
    for actor in (STUDENT_A1, STUDENT_A2, OUTSIDER):
        assert await _count_as(conn, actor, f"SELECT count(*) FROM {table}") == 0, actor
    # Course B's teacher sees only course B's rows.
    course_a_id = await conn.fetchval("SELECT id FROM app.courses WHERE slug = 'course-a'")
    seen = await _count_as(
        conn, TEACHER_B, f"SELECT count(*) FROM {table} WHERE course_id = $1", course_a_id
    )
    assert seen == 0


@pytest.mark.asyncio
async def test_student_cannot_exercise_teacher_write_policies_on_own_course(conn):
    """The teacher-write predicates must not have been widened to members.
    A student of course A can READ the course's concepts but cannot update or
    delete them, nor update the course row itself."""
    course_a_id = await conn.fetchval("SELECT id FROM app.courses WHERE slug = 'course-a'")

    tx = conn.transaction()
    await tx.start()
    try:
        await _as(conn, STUDENT_A1)
        assert await conn.fetchval("SELECT count(*) FROM app.concepts") == 1
        # UPDATE/DELETE under a non-matching USING is a silent 0-row no-op,
        # not an error -- assert on the affected-row tag.
        assert await conn.execute("UPDATE app.concepts SET display_name = 'hijacked'") == "UPDATE 0"
        assert (
            await conn.execute(
                "UPDATE app.courses SET name = 'hijacked' WHERE id = $1", course_a_id
            )
            == "UPDATE 0"
        )
        # INSERT is gated by WITH CHECK, which raises instead.
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await conn.execute(
                "INSERT INTO app.concepts "
                "(course_id, subject_slug, subject_display_name, slug, display_name) "
                "VALUES ($1, 'physics', 'Physics', 'sneaky', 'sneaky')",
                course_a_id,
            )
    finally:
        await tx.rollback()

    # The same statements DO land for the course's teacher.
    tx = conn.transaction()
    await tx.start()
    try:
        await _as(conn, TEACHER_A)
        assert await conn.execute("UPDATE app.concepts SET display_name = 'renamed'") == "UPDATE 1"
        new_id = await conn.fetchval(
            "INSERT INTO app.concepts "
            "(course_id, subject_slug, subject_display_name, slug, display_name) "
            "VALUES ($1, 'physics', 'Physics', 'teacher-made', 'teacher-made') RETURNING id",
            course_a_id,
        )
        assert new_id is not None
    finally:
        await tx.rollback()


@pytest.mark.asyncio
async def test_course_memberships_self_or_teacher_select_unchanged(conn):
    """The one policy DB-08d deliberately leaves on has_course_role still
    behaves: a student sees only their own membership row, the teacher sees
    every row of their course, and neither sees the other course."""
    assert await _count_as(conn, STUDENT_A1, "SELECT count(*) FROM app.course_memberships") == 1
    # Course A: STUDENT_A1 + STUDENT_A2 + TEACHER_A.
    assert await _count_as(conn, TEACHER_A, "SELECT count(*) FROM app.course_memberships") == 3
    assert await _count_as(conn, OUTSIDER, "SELECT count(*) FROM app.course_memberships") == 0


@pytest.mark.asyncio
async def test_learning_activities_teacher_reads_tutoring_only(conn):
    """The two permissive SELECT policies on app.learning_activities are left
    unmerged (see the migration header); their OR'd effect must be unchanged --
    the teacher sees students' TUTORING activities but not their chats."""
    tutoring = await _count_as(
        conn,
        TEACHER_A,
        "SELECT count(*) FROM app.learning_activities WHERE modality = 'tutoring'",
    )
    assert tutoring == 2  # both course-A students
    chats = await _count_as(
        conn, TEACHER_A, "SELECT count(*) FROM app.learning_activities WHERE modality = 'chat'"
    )
    assert chats == 0
    # And each student still sees exactly their own two activities.
    assert await _count_as(conn, STUDENT_A1, "SELECT count(*) FROM app.learning_activities") == 2


@pytest.mark.asyncio
async def test_chat_and_tutoring_message_visibility_across_roles(conn):
    """The nested-EXISTS policies, whose membership check binds to
    ``parent.course_id`` rather than the message row's own column."""
    # chat_messages: owner only -- not the co-student, not the teacher.
    assert await _count_as(conn, STUDENT_A1, "SELECT count(*) FROM app.chat_messages") == 1
    assert await _count_as(conn, TEACHER_A, "SELECT count(*) FROM app.chat_messages") == 0
    assert await _count_as(conn, OUTSIDER, "SELECT count(*) FROM app.chat_messages") == 0
    # tutoring_messages: owner OR course teacher.
    assert await _count_as(conn, STUDENT_A1, "SELECT count(*) FROM app.tutoring_messages") == 1
    assert await _count_as(conn, TEACHER_A, "SELECT count(*) FROM app.tutoring_messages") == 2
    # TEACHER_B teaches course B only: their one own-course row, none of A's.
    course_a_id = await conn.fetchval("SELECT id FROM app.courses WHERE slug = 'course-a'")
    assert await _count_as(conn, TEACHER_B, "SELECT count(*) FROM app.tutoring_messages") == 1
    assert (
        await _count_as(
            conn,
            TEACHER_B,
            "SELECT count(*) FROM app.tutoring_messages WHERE course_id = $1",
            course_a_id,
        )
        == 0
    )
    # question_opportunities: owner OR course teacher, via problem_attempts.
    assert await _count_as(conn, STUDENT_A1, "SELECT count(*) FROM app.question_opportunities") == 1
    assert await _count_as(conn, TEACHER_A, "SELECT count(*) FROM app.question_opportunities") == 2
    assert await _count_as(conn, OUTSIDER, "SELECT count(*) FROM app.question_opportunities") == 0


@pytest.mark.asyncio
async def test_no_claims_still_denies_everything(conn):
    """app_runtime with no request.jwt.claims resolves auth.uid() to NULL. The
    inline subquery must return no course ids, exactly as has_course_role
    returned false."""
    for table in ("app.courses", "app.problems", "app.student_progress", "app.uploads"):
        assert await _count_as(conn, None, f"SELECT count(*) FROM {table}") == 0, table


# ===========================================================================
# 3. Plan shape -- the reason the migration exists.
# ===========================================================================


@pytest.mark.asyncio
async def test_membership_check_plans_as_a_hashed_subplan_not_per_row(conn):
    """The old ``(SELECT internal.has_course_role(<col>, ...))`` form is a
    CORRELATED scalar sub-SELECT, so Postgres plans it as a per-row SubPlan
    over an opaque SECURITY DEFINER call. The inline form is uncorrelated, so
    the planner hashes it once per query. If someone re-correlates one of
    these expressions this assertion is what catches it."""
    tx = conn.transaction()
    await tx.start()
    try:
        await _as(conn, TEACHER_A)
        lines = [
            row[0] for row in await conn.fetch("EXPLAIN (COSTS OFF) SELECT id FROM app.problems")
        ]
    finally:
        await tx.rollback()

    plan = "\n".join(lines)
    scan_index = next(i for i, line in enumerate(lines) if "on problems" in line)
    # The filter attached to the app.problems scan itself -- the one evaluated
    # once per candidate row -- must be the hashed (build-once) form.
    assert "hashed SubPlan" in lines[scan_index + 1], plan
    # ...and it is the inline membership subquery that got hashed.
    assert "course_memberships" in plan, plan
