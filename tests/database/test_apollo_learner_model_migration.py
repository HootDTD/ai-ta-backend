"""Real-Postgres behavioral tests for migration 026 (Apollo learner model).

WU-3A ships migration ``026_apollo_learner_model.sql``: course-scoping
``apollo_subjects.search_space_id`` (with a two-step backfill), the Layer-1
skill inventory (``apollo_kg_entities`` / ``apollo_entity_prereqs``), the
Layer-3 learner model (``apollo_learner_state`` /``apollo_mastery_events`` with
``UNIQUE NULLS NOT DISTINCT``), the grading-core audit tables
(``apollo_graph_comparison_runs`` / ``_findings``), and
``apollo_problem_attempts.learner_update_pending``.

The value of this unit is DDL behavior — FK cascades, CHECK constraints,
``NULLS NOT DISTINCT`` dedup, the backfill landmine — none of which SQLite can
honor. So these tests apply the *actual* migration chain + 026 to a fresh
database on the session pgvector container (Testcontainers) and exercise every
constraint branch for real, mirroring
``tests/database/test_apollo_attempt_result_constraint.py`` and
``tests/database/test_teacher_uploads_constraints.py``.

Migration selection is content-scoped: every on-disk migration that creates or
alters ``apollo_subjects`` / ``apollo_concepts`` / ``apollo_problem_attempts``
(the three FK targets 026 needs) joins the chain automatically (009, 018, 025),
then 026. 026 does not depend on the 023 ``apollo_sessions`` auth retrofit, so
the chain omits it — only the FK-stub targets (``auth.users``,
``aita_search_spaces``) are stubbed.
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
MIGRATED_DB_NAME = "apollo_learner_model_migrations"
PRE026_DB_PREFIX = "apollo_learner_model_pre026"

# FK targets 026 references that no on-disk migration creates: auth.users is
# Supabase-managed; aita_search_spaces comes from the app's model metadata in
# prod. We stub them with the minimal shape the FKs resolve against.
_STUB_DDL = """
CREATE SCHEMA IF NOT EXISTS auth;
CREATE TABLE auth.users (id UUID PRIMARY KEY);
CREATE TABLE aita_search_spaces (id SERIAL PRIMARY KEY);
"""

# Content scope: the three tables 026 hangs FKs / the backfill column off.
_TOUCHES_TARGETS = re.compile(
    r"(CREATE TABLE IF NOT EXISTS|CREATE TABLE|ALTER TABLE)\s+"
    r"apollo_(subjects|concepts|problem_attempts)\b"
)
_MIGRATION_026 = MIGRATIONS_DIR / "026_apollo_learner_model.sql"
# Later migrations that ALTER apollo_problem_attempts but DEPEND on 026's columns
# must be excluded from this chain: the harness applies 026 LAST (separately), so a
# migration that references 026's additions would run before 026 here and fail.
# 028 (WU-5B3a-0) adds a partial index whose predicate references
# `learner_update_pending` — a column 026 creates — so it is excluded here. 028
# has its own forward-chain test (test_apollo_learner_janitor_migration.py).
_EXCLUDE_FROM_CHAIN = frozenset({
    _MIGRATION_026.name,
    "028_apollo_learner_janitor.sql",
})


def _chain_migrations() -> list[Path]:
    """Every migration defining/altering apollo_subjects/concepts/problem_attempts,
    in filename order. Excludes 026 itself (applied separately) and any later
    migration that depends on 026's columns (see _EXCLUDE_FROM_CHAIN)."""
    return [
        path
        for path in sorted(MIGRATIONS_DIR.glob("*.sql"))
        if path.name not in _EXCLUDE_FROM_CHAIN
        and _TOUCHES_TARGETS.search(path.read_text(encoding="utf-8"))
    ]


def _plain_dsn(sqlalchemy_url: str, database: str) -> str:
    url = make_url(sqlalchemy_url).set(drivername="postgresql", database=database)
    return url.render_as_string(hide_password=False)


async def _create_db(admin_dsn: str, name: str) -> None:
    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()


async def _apply_chain(dsn: str, *, include_026: bool) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(_STUB_DDL)
        for migration in _chain_migrations():
            await conn.execute(migration.read_text(encoding="utf-8"))
        if include_026:
            await conn.execute(_MIGRATION_026.read_text(encoding="utf-8"))
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Fully-migrated DB (chain + 026), shared across most tests via per-test
# transaction rollback. The backfill-specific tests build their own pre-026
# databases (a populated table BEFORE 026 is the whole point) below.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _migrated_dsn(_pg_url: str):
    base_db = make_url(_pg_url).database
    assert base_db, "container URL has no database name"
    admin_dsn = _plain_dsn(_pg_url, base_db)
    migrated_dsn = _plain_dsn(_pg_url, MIGRATED_DB_NAME)

    async def _setup() -> None:
        await _create_db(admin_dsn, MIGRATED_DB_NAME)
        await _apply_chain(migrated_dsn, include_026=True)

    asyncio.run(_setup())
    yield migrated_dsn


@pytest_asyncio.fixture
async def mig_conn(_migrated_dsn: str):
    """Connection to the fully-migrated DB; everything rolls back per test."""
    conn = await asyncpg.connect(_migrated_dsn)
    tr = conn.transaction()
    await tr.start()
    try:
        yield conn
    finally:
        await tr.rollback()
        await conn.close()


async def _expect_violation(conn: asyncpg.Connection, error_type, coro) -> None:
    """Assert ``coro`` raises ``error_type`` WITHOUT poisoning ``conn``.

    In Postgres a failed statement aborts the whole transaction: every
    subsequent command raises ``InFailedSQLTransactionError`` until rollback.
    ``mig_conn`` wraps each test in ONE transaction, so a bare
    ``with pytest.raises(...): await <violating insert>`` would leave the
    connection unusable for the rest of the test (the exact cascade that broke
    the multi-assertion tests here). Wrapping the violation in a SAVEPOINT
    (asyncpg nested transaction) and rolling back TO that savepoint clears the
    error state, so the test can keep issuing SQL on the same connection.
    """
    sp = conn.transaction()
    await sp.start()
    try:
        with pytest.raises(error_type):
            await coro
    finally:
        # ROLLBACK TO SAVEPOINT clears the aborted-statement state whether the
        # violation fired (expected) or not (re-raises the pytest failure).
        await sp.rollback()


# ---- seed helpers (one course -> subject -> concept -> entity to hang rows off)


async def _seed_space(conn: asyncpg.Connection) -> int:
    return await conn.fetchval("INSERT INTO aita_search_spaces DEFAULT VALUES RETURNING id")


async def _seed_user(conn: asyncpg.Connection, marker: int = 1) -> str:
    uid = f"{marker:08d}-0000-4000-8000-000000000001"
    await conn.execute("INSERT INTO auth.users (id) VALUES ($1)", uid)
    return uid


async def _seed_subject(conn: asyncpg.Connection, space_id: int, slug: str = "s1") -> int:
    return await conn.fetchval(
        "INSERT INTO apollo_subjects (slug, display_name, search_space_id) "
        "VALUES ($1, 'Subj', $2) RETURNING id",
        slug,
        space_id,
    )


async def _seed_concept(conn: asyncpg.Connection, subject_id: int, slug: str = "c1") -> int:
    return await conn.fetchval(
        "INSERT INTO apollo_concepts (subject_id, slug, display_name) "
        "VALUES ($1, $2, 'Concept') RETURNING id",
        subject_id,
        slug,
    )


async def _seed_entity(
    conn: asyncpg.Connection,
    concept_id: int,
    canonical_key: str = "k1",
    kind: str = "concept",
) -> int:
    return await conn.fetchval(
        "INSERT INTO apollo_kg_entities (concept_id, canonical_key, kind, display_name) "
        "VALUES ($1, $2, $3, 'Ent') RETURNING id",
        concept_id,
        canonical_key,
        kind,
    )


async def _seed_session(conn: asyncpg.Connection) -> int:
    return await conn.fetchval(
        "INSERT INTO apollo_sessions (student_id, concept_cluster_id) "
        "VALUES ('stu-1', 'fluid_mechanics') RETURNING id"
    )


async def _seed_attempt(conn: asyncpg.Connection, session_id: int) -> int:
    return await conn.fetchval(
        "INSERT INTO apollo_problem_attempts (session_id, problem_id, difficulty) "
        "VALUES ($1, 'p1', 'intro') RETURNING id",
        session_id,
    )


async def _seed_chain(conn: asyncpg.Connection, marker: int = 1):
    """Return (space_id, user_id, concept_id, entity_id) for a single course.

    ``marker`` disambiguates every UNIQUE/PK value (user UUID, subject slug,
    concept slug, entity canonical_key) so multiple chains can coexist inside
    ONE ``mig_conn`` transaction. apollo_subjects.slug and auth.users.id are
    unique, so two default chains in the same test would otherwise collide.
    """
    space = await _seed_space(conn)
    user = await _seed_user(conn, marker=marker)
    subject = await _seed_subject(conn, space, slug=f"s{marker}")
    concept = await _seed_concept(conn, subject, slug=f"c{marker}")
    entity = await _seed_entity(conn, concept, canonical_key=f"k{marker}")
    return space, user, concept, entity


async def _insert_learner_state(
    conn: asyncpg.Connection,
    user_id: str,
    space_id: int,
    entity_id: int,
    *,
    belief=(0.2, 0.3, 0.5),
    mastery: float = 0.5,
    confidence: float = 0.5,
) -> None:
    await conn.execute(
        "INSERT INTO apollo_learner_state "
        "(user_id, search_space_id, entity_id, belief, mastery, confidence) "
        "VALUES ($1, $2, $3, $4, $5, $6)",
        user_id,
        space_id,
        entity_id,
        list(belief),
        mastery,
        confidence,
    )


async def _insert_mastery_event(
    conn: asyncpg.Connection,
    user_id: str,
    space_id: int,
    entity_id: int,
    *,
    attempt_id=None,
    event_kind: str = "covered",
    score=None,
    parser_confidence=None,
    grader_confidence=None,
    prior=(0.3, 0.3, 0.4),
    posterior=(0.2, 0.3, 0.5),
    mastery_after: float = 0.5,
) -> int:
    return await conn.fetchval(
        "INSERT INTO apollo_mastery_events "
        "(user_id, search_space_id, entity_id, attempt_id, event_kind, score, "
        " parser_confidence, grader_confidence, prior_belief, posterior_belief, "
        " mastery_after) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) RETURNING id",
        user_id,
        space_id,
        entity_id,
        attempt_id,
        event_kind,
        score,
        parser_confidence,
        grader_confidence,
        list(prior),
        list(posterior),
        mastery_after,
    )


async def _insert_run(
    conn: asyncpg.Connection,
    attempt_id: int,
    user_id: str,
    space_id: int,
    *,
    comparison_version: str = "v1",
) -> int:
    return await conn.fetchval(
        "INSERT INTO apollo_graph_comparison_runs "
        "(attempt_id, user_id, search_space_id, coverage_score, soundness_score, "
        " bisimilarity_score, normalization_confidence, comparison_version, "
        " reference_graph_hash) "
        "VALUES ($1,$2,$3,0.5,0.5,0.5,0.9,$4,'hash1') RETURNING id",
        attempt_id,
        user_id,
        space_id,
        comparison_version,
    )


# ---------------------------------------------------------------------------
# Table creation + schema shape
# ---------------------------------------------------------------------------

_NEW_TABLES = (
    "apollo_kg_entities",
    "apollo_entity_prereqs",
    "apollo_learner_state",
    "apollo_mastery_events",
    "apollo_graph_comparison_runs",
    "apollo_graph_comparison_findings",
)


async def test_all_eight_tables_created(mig_conn):
    """All 6 new tables + the 2 altered tables exist after chain+026."""
    for table in _NEW_TABLES:
        regclass = await mig_conn.fetchval("SELECT to_regclass($1)", f"public.{table}")
        assert regclass is not None, f"{table} missing"
    # The two altered tables and their new columns also exist.
    assert await mig_conn.fetchval("SELECT to_regclass('public.apollo_subjects')")
    assert await mig_conn.fetchval("SELECT to_regclass('public.apollo_problem_attempts')")


async def test_subjects_search_space_id_not_null_and_fk(mig_conn):
    is_nullable = await mig_conn.fetchval(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name='apollo_subjects' AND column_name='search_space_id'"
    )
    assert is_nullable == "NO"
    fk = await mig_conn.fetchval(
        """
        SELECT 1 FROM information_schema.table_constraints tc
        JOIN information_schema.constraint_column_usage ccu
          ON tc.constraint_name = ccu.constraint_name
        WHERE tc.table_name = 'apollo_subjects'
          AND tc.constraint_type = 'FOREIGN KEY'
          AND ccu.table_name = 'aita_search_spaces'
        """
    )
    assert fk == 1


# ---------------------------------------------------------------------------
# Backfill landmine (pre-026 populated DB) — builds its own databases
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pre026_factory(_pg_url: str):
    """Build a fresh pre-026 DB (chain WITHOUT 026), let the test seed it, then
    apply 026. Yields an async builder; cleans up the databases afterwards."""
    base_db = make_url(_pg_url).database
    admin_dsn = _plain_dsn(_pg_url, base_db)
    created: list[str] = []

    async def _build(seed_coro):
        name = f"{PRE026_DB_PREFIX}_{len(created)}"
        created.append(name)
        dsn = _plain_dsn(_pg_url, name)
        await _create_db(admin_dsn, name)
        await _apply_chain(dsn, include_026=False)
        conn = await asyncpg.connect(dsn)
        try:
            await seed_coro(conn)
        finally:
            await conn.close()
        # Now apply 026 on the populated pre-026 DB.
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(_MIGRATION_026.read_text(encoding="utf-8"))
        finally:
            await conn.close()
        return dsn

    async def _build_expect_error(seed_coro):
        name = f"{PRE026_DB_PREFIX}_{len(created)}"
        created.append(name)
        dsn = _plain_dsn(_pg_url, name)
        await _create_db(admin_dsn, name)
        await _apply_chain(dsn, include_026=False)
        conn = await asyncpg.connect(dsn)
        try:
            await seed_coro(conn)
            with pytest.raises(asyncpg.PostgresError) as exc:
                await conn.execute(_MIGRATION_026.read_text(encoding="utf-8"))
            return exc.value
        finally:
            await conn.close()

    _build.expect_error = _build_expect_error  # type: ignore[attr-defined]
    try:
        yield _build
    finally:
        admin = await asyncpg.connect(admin_dsn)
        try:
            for name in created:
                await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        finally:
            await admin.close()


async def test_subjects_backfill_populates_existing_global_rows(pre026_factory):
    """Pre-026 DB with a subject row that has NO search_space_id: 026 backfills it
    to MIN(spaces.id) and tightens to NOT NULL. Proves a populated DB survives."""

    async def _seed(conn):
        space = await conn.fetchval("INSERT INTO aita_search_spaces DEFAULT VALUES RETURNING id")
        # pre-026 apollo_subjects has no search_space_id column.
        await conn.execute(
            "INSERT INTO apollo_subjects (slug, display_name) VALUES ('legacy', 'Legacy')"
        )
        return space

    dsn = await pre026_factory(_seed)
    conn = await asyncpg.connect(dsn)
    try:
        space_min = await conn.fetchval("SELECT MIN(id) FROM aita_search_spaces")
        ssid = await conn.fetchval(
            "SELECT search_space_id FROM apollo_subjects WHERE slug='legacy'"
        )
        assert ssid == space_min
        is_nullable = await conn.fetchval(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name='apollo_subjects' AND column_name='search_space_id'"
        )
        assert is_nullable == "NO"
    finally:
        await conn.close()


async def test_subjects_backfill_guard_raises_when_no_course(pre026_factory):
    """Pre-026 DB with a subject row but ZERO aita_search_spaces rows: 026 raises
    (the Phase C guard fires) rather than silently dropping curriculum."""

    async def _seed(conn):
        await conn.execute(
            "INSERT INTO apollo_subjects (slug, display_name) VALUES ('orphan', 'Orphan')"
        )

    err = await pre026_factory.expect_error(_seed)
    assert "cannot be attributed to a course" in str(err)


async def test_subjects_backfill_noop_on_empty(pre026_factory):
    """Empty apollo_subjects + empty course table: 026 applies cleanly, NOT NULL,
    no rows."""

    async def _seed(conn):  # nothing seeded
        return None

    dsn = await pre026_factory(_seed)
    conn = await asyncpg.connect(dsn)
    try:
        count = await conn.fetchval("SELECT count(*) FROM apollo_subjects")
        assert count == 0
        is_nullable = await conn.fetchval(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name='apollo_subjects' AND column_name='search_space_id'"
        )
        assert is_nullable == "NO"
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Layer 1 — apollo_kg_entities + apollo_entity_prereqs
# ---------------------------------------------------------------------------


async def test_kg_entities_unique_is_per_concept_not_global(mig_conn):
    """Same canonical_key under TWO different concepts inserts fine; the same
    (concept_id, canonical_key) twice raises. Guards §1.4 per-concept uniqueness."""
    space = await _seed_space(mig_conn)
    subject = await _seed_subject(mig_conn, space)
    concept_a = await _seed_concept(mig_conn, subject, "ca")
    concept_b = await _seed_concept(mig_conn, subject, "cb")

    await _seed_entity(mig_conn, concept_a, "shared_key")
    # Same key, different concept -> allowed (NOT global unique).
    await _seed_entity(mig_conn, concept_b, "shared_key")

    rows = await mig_conn.fetchval(
        "SELECT count(*) FROM apollo_kg_entities WHERE canonical_key='shared_key'"
    )
    assert rows == 2

    await _expect_violation(
        mig_conn,
        asyncpg.UniqueViolationError,
        _seed_entity(mig_conn, concept_a, "shared_key"),
    )


async def test_kg_entities_kind_check_rejects_bad_accepts_good(mig_conn):
    from apollo.persistence.models import ENTITY_KINDS

    space = await _seed_space(mig_conn)
    subject = await _seed_subject(mig_conn, space)
    concept = await _seed_concept(mig_conn, subject)

    for i, kind in enumerate(ENTITY_KINDS):
        await _seed_entity(mig_conn, concept, f"k{i}", kind)

    await _expect_violation(
        mig_conn,
        asyncpg.CheckViolationError,
        _seed_entity(mig_conn, concept, "kbad", "banana"),
    )


async def test_kg_entities_fk_concept_cascade(mig_conn):
    space = await _seed_space(mig_conn)
    subject = await _seed_subject(mig_conn, space)
    concept = await _seed_concept(mig_conn, subject)
    await _seed_entity(mig_conn, concept)

    await mig_conn.execute("DELETE FROM apollo_concepts WHERE id=$1", concept)
    remaining = await mig_conn.fetchval(
        "SELECT count(*) FROM apollo_kg_entities WHERE concept_id=$1", concept
    )
    assert remaining == 0


async def test_entity_prereqs_composite_pk_and_cascade(mig_conn):
    space = await _seed_space(mig_conn)
    subject = await _seed_subject(mig_conn, space)
    concept = await _seed_concept(mig_conn, subject)
    e_from = await _seed_entity(mig_conn, concept, "from")
    e_to = await _seed_entity(mig_conn, concept, "to")

    await mig_conn.execute(
        "INSERT INTO apollo_entity_prereqs (from_entity_id, to_entity_id) VALUES ($1,$2)",
        e_from,
        e_to,
    )
    # Duplicate composite PK raises.
    await _expect_violation(
        mig_conn,
        asyncpg.UniqueViolationError,
        mig_conn.execute(
            "INSERT INTO apollo_entity_prereqs (from_entity_id, to_entity_id) VALUES ($1,$2)",
            e_from,
            e_to,
        ),
    )
    # Deleting an entity cascades the edge (to_entity_id direction).
    await mig_conn.execute("DELETE FROM apollo_kg_entities WHERE id=$1", e_to)
    edges = await mig_conn.fetchval("SELECT count(*) FROM apollo_entity_prereqs")
    assert edges == 0


# ---------------------------------------------------------------------------
# Layer 3 — apollo_learner_state
# ---------------------------------------------------------------------------


async def test_learner_state_pk_blocks_duplicate_tuple(mig_conn):
    space, user, _concept, entity = await _seed_chain(mig_conn)
    await _insert_learner_state(mig_conn, user, space, entity)
    await _expect_violation(
        mig_conn,
        asyncpg.UniqueViolationError,
        _insert_learner_state(mig_conn, user, space, entity),
    )


async def test_learner_state_belief_array_check(mig_conn):
    space, user, _concept, entity = await _seed_chain(mig_conn)
    # length 3 ok
    await _insert_learner_state(mig_conn, user, space, entity, belief=(0.2, 0.3, 0.5))
    # length 2 and length 4 both raise (each in its own savepoint, so the
    # CHECK failure does not abort the surrounding test transaction).
    await _expect_violation(
        mig_conn,
        asyncpg.CheckViolationError,
        _insert_learner_state(mig_conn, user, space, entity, belief=(0.5, 0.5)),
    )
    await _expect_violation(
        mig_conn,
        asyncpg.CheckViolationError,
        _insert_learner_state(mig_conn, user, space, entity, belief=(0.1, 0.2, 0.3, 0.4)),
    )


async def test_learner_state_mastery_confidence_range_checks(mig_conn):
    space, user, _concept, entity = await _seed_chain(mig_conn)
    await _insert_learner_state(mig_conn, user, space, entity, mastery=0.0, confidence=1.0)
    await _expect_violation(
        mig_conn,
        asyncpg.CheckViolationError,
        _insert_learner_state(mig_conn, user, space, entity, mastery=-0.1),
    )
    await _expect_violation(
        mig_conn,
        asyncpg.CheckViolationError,
        _insert_learner_state(mig_conn, user, space, entity, confidence=1.1),
    )


async def test_learner_state_fk_cascades(mig_conn):
    # Each of the three FKs cascades the learner_state row away. Distinct markers
    # keep the three chains from colliding on subject.slug / auth.users.id inside
    # this single transaction; each DELETE drives the row count back to 0.
    # user delete
    space, user, _concept, entity = await _seed_chain(mig_conn, marker=1)
    await _insert_learner_state(mig_conn, user, space, entity)
    await mig_conn.execute("DELETE FROM auth.users WHERE id=$1", user)
    assert await mig_conn.fetchval("SELECT count(*) FROM apollo_learner_state") == 0

    # search_space delete
    space, user, _concept, entity = await _seed_chain(mig_conn, marker=2)
    await _insert_learner_state(mig_conn, user, space, entity)
    await mig_conn.execute("DELETE FROM aita_search_spaces WHERE id=$1", space)
    assert await mig_conn.fetchval("SELECT count(*) FROM apollo_learner_state") == 0

    # entity delete
    space, user, _concept, entity = await _seed_chain(mig_conn, marker=3)
    await _insert_learner_state(mig_conn, user, space, entity)
    await mig_conn.execute("DELETE FROM apollo_kg_entities WHERE id=$1", entity)
    assert await mig_conn.fetchval("SELECT count(*) FROM apollo_learner_state") == 0


# ---------------------------------------------------------------------------
# Layer 3 — apollo_mastery_events
# ---------------------------------------------------------------------------


async def test_mastery_events_belief_checks(mig_conn):
    space, user, _concept, entity = await _seed_chain(mig_conn)
    await _insert_mastery_event(mig_conn, user, space, entity)
    await _expect_violation(
        mig_conn,
        asyncpg.CheckViolationError,
        _insert_mastery_event(mig_conn, user, space, entity, prior=(0.5, 0.5)),
    )
    await _expect_violation(
        mig_conn,
        asyncpg.CheckViolationError,
        _insert_mastery_event(mig_conn, user, space, entity, posterior=(0.1, 0.2, 0.3, 0.4)),
    )


async def test_mastery_events_score_confidence_range_checks(mig_conn):
    space, user, _concept, entity = await _seed_chain(mig_conn)
    # null + in-range all accepted
    await _insert_mastery_event(
        mig_conn,
        user,
        space,
        entity,
        score=None,
        parser_confidence=0.0,
        grader_confidence=1.0,
        mastery_after=0.5,
    )
    await _expect_violation(
        mig_conn,
        asyncpg.CheckViolationError,
        _insert_mastery_event(mig_conn, user, space, entity, score=1.5),
    )
    await _expect_violation(
        mig_conn,
        asyncpg.CheckViolationError,
        _insert_mastery_event(mig_conn, user, space, entity, parser_confidence=-0.1),
    )
    await _expect_violation(
        mig_conn,
        asyncpg.CheckViolationError,
        _insert_mastery_event(mig_conn, user, space, entity, grader_confidence=2.0),
    )
    await _expect_violation(
        mig_conn,
        asyncpg.CheckViolationError,
        _insert_mastery_event(mig_conn, user, space, entity, mastery_after=-0.5),
    )


async def test_mastery_events_nulls_not_distinct_dedup(mig_conn):
    """Two rows with the SAME (entity_id, event_kind) and attempt_id=NULL: the
    second raises. A plain UNIQUE would NOT — this is the PG15+ NULLS NOT
    DISTINCT behavior. A control with two DIFFERENT non-null attempt_ids inserts
    both."""
    space, user, _concept, entity = await _seed_chain(mig_conn)
    await _insert_mastery_event(
        mig_conn, user, space, entity, attempt_id=None, event_kind="covered"
    )
    await _expect_violation(
        mig_conn,
        asyncpg.UniqueViolationError,
        _insert_mastery_event(mig_conn, user, space, entity, attempt_id=None, event_kind="covered"),
    )

    # Control: distinct non-null attempt_ids both insert.
    sess = await _seed_session(mig_conn)
    a1 = await _seed_attempt(mig_conn, sess)
    a2 = await _seed_attempt(mig_conn, sess)
    await _insert_mastery_event(mig_conn, user, space, entity, attempt_id=a1, event_kind="partial")
    await _insert_mastery_event(mig_conn, user, space, entity, attempt_id=a2, event_kind="partial")
    n = await mig_conn.fetchval(
        "SELECT count(*) FROM apollo_mastery_events WHERE event_kind='partial'"
    )
    assert n == 2


async def test_mastery_events_attempt_id_set_null_on_delete(mig_conn):
    """Deleting the attempt sets attempt_id NULL but keeps the event (the log
    outlives the attempt)."""
    space, user, _concept, entity = await _seed_chain(mig_conn)
    sess = await _seed_session(mig_conn)
    attempt = await _seed_attempt(mig_conn, sess)
    event_id = await _insert_mastery_event(
        mig_conn, user, space, entity, attempt_id=attempt, event_kind="covered"
    )
    await mig_conn.execute("DELETE FROM apollo_problem_attempts WHERE id=$1", attempt)
    row = await mig_conn.fetchrow(
        "SELECT attempt_id FROM apollo_mastery_events WHERE id=$1", event_id
    )
    assert row is not None
    assert row["attempt_id"] is None


async def test_mastery_events_entity_cascade(mig_conn):
    space, user, _concept, entity = await _seed_chain(mig_conn)
    await _insert_mastery_event(mig_conn, user, space, entity)
    await mig_conn.execute("DELETE FROM apollo_kg_entities WHERE id=$1", entity)
    assert await mig_conn.fetchval("SELECT count(*) FROM apollo_mastery_events") == 0


# ---------------------------------------------------------------------------
# Grading-core audit — runs + findings
# ---------------------------------------------------------------------------


async def test_comparison_runs_unique_attempt_version(mig_conn):
    space, user, _concept, _entity = await _seed_chain(mig_conn)
    sess = await _seed_session(mig_conn)
    attempt = await _seed_attempt(mig_conn, sess)
    await _insert_run(mig_conn, attempt, user, space, comparison_version="v1")
    await _expect_violation(
        mig_conn,
        asyncpg.UniqueViolationError,
        _insert_run(mig_conn, attempt, user, space, comparison_version="v1"),
    )
    # Different version inserts.
    await _insert_run(mig_conn, attempt, user, space, comparison_version="v2")
    n = await mig_conn.fetchval(
        "SELECT count(*) FROM apollo_graph_comparison_runs WHERE attempt_id=$1", attempt
    )
    assert n == 2


async def test_comparison_runs_attempt_cascade(mig_conn):
    space, user, _concept, entity = await _seed_chain(mig_conn)
    sess = await _seed_session(mig_conn)
    attempt = await _seed_attempt(mig_conn, sess)
    run = await _insert_run(mig_conn, attempt, user, space)
    await mig_conn.execute(
        "INSERT INTO apollo_graph_comparison_findings (run_id, entity_id, finding_kind) "
        "VALUES ($1, $2, 'covered_node')",
        run,
        entity,
    )
    await mig_conn.execute("DELETE FROM apollo_problem_attempts WHERE id=$1", attempt)
    assert await mig_conn.fetchval("SELECT count(*) FROM apollo_graph_comparison_runs") == 0
    assert await mig_conn.fetchval("SELECT count(*) FROM apollo_graph_comparison_findings") == 0


async def test_comparison_findings_entity_set_null_on_delete(mig_conn):
    """A pruned entity must NOT delete the audit finding — entity_id degrades to
    NULL."""
    space, user, _concept, entity = await _seed_chain(mig_conn)
    sess = await _seed_session(mig_conn)
    attempt = await _seed_attempt(mig_conn, sess)
    run = await _insert_run(mig_conn, attempt, user, space)
    finding_id = await mig_conn.fetchval(
        "INSERT INTO apollo_graph_comparison_findings (run_id, entity_id, finding_kind) "
        "VALUES ($1, $2, 'covered_node') RETURNING id",
        run,
        entity,
    )
    await mig_conn.execute("DELETE FROM apollo_kg_entities WHERE id=$1", entity)
    row = await mig_conn.fetchrow(
        "SELECT entity_id FROM apollo_graph_comparison_findings WHERE id=$1", finding_id
    )
    assert row is not None
    assert row["entity_id"] is None


async def test_comparison_findings_run_cascade(mig_conn):
    space, user, _concept, entity = await _seed_chain(mig_conn)
    sess = await _seed_session(mig_conn)
    attempt = await _seed_attempt(mig_conn, sess)
    run = await _insert_run(mig_conn, attempt, user, space)
    await mig_conn.execute(
        "INSERT INTO apollo_graph_comparison_findings (run_id, finding_kind) "
        "VALUES ($1, 'missing_node')",
        run,
    )
    await mig_conn.execute("DELETE FROM apollo_graph_comparison_runs WHERE id=$1", run)
    assert await mig_conn.fetchval("SELECT count(*) FROM apollo_graph_comparison_findings") == 0


# ---------------------------------------------------------------------------
# learner_update_pending
# ---------------------------------------------------------------------------


async def test_learner_update_pending_defaults_false(mig_conn):
    sess = await _seed_session(mig_conn)
    # Insert omitting the new column -> server_default false.
    attempt = await _seed_attempt(mig_conn, sess)
    val = await mig_conn.fetchval(
        "SELECT learner_update_pending FROM apollo_problem_attempts WHERE id=$1", attempt
    )
    assert val is False


# ---------------------------------------------------------------------------
# Access-control / isolation invariant (positive, negative, cascade)
# ---------------------------------------------------------------------------


async def _seed_two_courses(conn: asyncpg.Connection):
    """Two independent courses, each with its own user/subject/concept/entity and
    a learner_state row. Returns the two tuples."""
    s1 = await _seed_space(conn)
    u1 = await _seed_user(conn, marker=10)
    subj1 = await _seed_subject(conn, s1, "subj1")
    con1 = await _seed_concept(conn, subj1, "con1")
    ent1 = await _seed_entity(conn, con1, "e1")
    await _insert_learner_state(conn, u1, s1, ent1)

    s2 = await _seed_space(conn)
    u2 = await _seed_user(conn, marker=20)
    subj2 = await _seed_subject(conn, s2, "subj2")
    con2 = await _seed_concept(conn, subj2, "con2")
    ent2 = await _seed_entity(conn, con2, "e2")
    await _insert_learner_state(conn, u2, s2, ent2)
    return (s1, u1, ent1), (s2, u2, ent2)


async def test_learner_state_scoped_select_returns_rows(mig_conn):
    (s1, u1, _e1), _course2 = await _seed_two_courses(mig_conn)
    rows = await mig_conn.fetch(
        "SELECT * FROM apollo_learner_state WHERE search_space_id=$1 AND user_id=$2",
        s1,
        u1,
    )
    assert len(rows) == 1


async def test_learner_state_other_tenant_returns_zero_rows(mig_conn):
    (s1, u1, _e1), (s2, u2, _e2) = await _seed_two_courses(mig_conn)
    # A different course id and a different user each return zero for course-1's data.
    other_course = await mig_conn.fetch(
        "SELECT * FROM apollo_learner_state WHERE search_space_id=$1 AND user_id=$2",
        s2,
        u1,
    )
    assert other_course == []
    other_user = await mig_conn.fetch(
        "SELECT * FROM apollo_learner_state WHERE search_space_id=$1 AND user_id=$2",
        s1,
        u2,
    )
    assert other_user == []


async def test_course_delete_cascades_only_its_curriculum(mig_conn):
    (s1, _u1, _e1), (s2, _u2, _e2) = await _seed_two_courses(mig_conn)
    await mig_conn.execute("DELETE FROM aita_search_spaces WHERE id=$1", s1)
    # course-1's chain is gone, course-2's intact.
    assert (
        await mig_conn.fetchval(
            "SELECT count(*) FROM apollo_learner_state WHERE search_space_id=$1", s1
        )
        == 0
    )
    assert (
        await mig_conn.fetchval("SELECT count(*) FROM apollo_subjects WHERE search_space_id=$1", s1)
        == 0
    )
    assert (
        await mig_conn.fetchval(
            "SELECT count(*) FROM apollo_learner_state WHERE search_space_id=$1", s2
        )
        == 1
    )
    assert (
        await mig_conn.fetchval("SELECT count(*) FROM apollo_subjects WHERE search_space_id=$1", s2)
        == 1
    )
