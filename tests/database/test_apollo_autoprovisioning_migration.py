"""Real-Postgres behavioral tests for migration 030 (Apollo auto-provisioning).

WU-3B2a ships migration ``030_apollo_autoprovisioning.sql``: the §8B
materials->Apollo auto-provisioning substrate. It adds five columns to
``apollo_concept_problems`` (``tier``/``solution_source``/``provenance``/
``quarantined_at``/``search_space_id``) with two in-migration backfills, one
column to ``apollo_kg_entities`` (``scope_summary``), five new course-scoped
observability/queue tables (``apollo_ingest_runs`` / ``apollo_provisioning_jobs``
/ ``apollo_rejected_problems`` / ``apollo_dedup_decisions`` /
``apollo_ingest_errors``), an RLS stopgap on the five new tables, and a
``(concept_id, tier)`` index.

The value of this unit is DDL behavior — CHECK constraints, FK cascades, the
partial-unique-index idempotent-enqueue collapse, the RLS stopgap, and the two
backfills (the tier=2 bernoulli-pool-survival flip + the denormalized
search_space_id) — none of which SQLite can honor. So these tests apply the
*actual* migration chain + 030 to a fresh database on the session pgvector
container (Testcontainers) and exercise every constraint branch for real,
mirroring ``tests/database/test_apollo_learner_model_migration.py``.

Migration selection is content-scoped: every on-disk migration that creates or
alters ``apollo_subjects`` / ``apollo_concepts`` / ``apollo_concept_problems`` /
``apollo_kg_entities`` / ``apollo_problem_attempts`` (the FK / backfill-join
targets 030 needs, plus the transitive prerequisites of the in-chain 026) joins
the chain automatically: 009 creates sessions/problem_attempts, 018 creates
concepts/concept_problems, 026 creates kg_entities AND adds
``apollo_subjects.search_space_id`` that backfill (a)'s join reads. The resolved
chain is [009, 018, 025, 026]; 030 is applied LAST and 028 (forward-dependent on
026's column) is excluded. ``test_chain_order_018_before_026_before_030`` PROVES
the resolved ordering rather than assuming it.
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
MIGRATED_DB_NAME = "apollo_autoprovisioning_migrations"
PRE030_DB_PREFIX = "apollo_autoprovisioning_pre030"

# FK targets 030 references that no on-disk migration creates: auth.users is
# Supabase-managed; aita_search_spaces comes from the app's model metadata in
# prod. We stub them with the minimal shape the FKs resolve against. (030's five
# new tables FK aita_search_spaces; the chain's 018/026 also reference these.)
_STUB_DDL = """
CREATE SCHEMA IF NOT EXISTS auth;
CREATE TABLE auth.users (id UUID PRIMARY KEY);
CREATE TABLE aita_search_spaces (id SERIAL PRIMARY KEY);
"""

# Content scope: every table 030 hangs FKs off / ALTERs / whose backfill-join it
# reads, PLUS the transitive prerequisites of the chain members. 030 ALTERs
# apollo_concept_problems + apollo_kg_entities and FKs the five new tables to
# aita_search_spaces / apollo_concepts / apollo_kg_entities, and backfill (a)
# joins apollo_subjects.search_space_id (added by 026). So the chain must include
# 018 (creates concepts/concept_problems) and 026 (creates kg_entities + adds
# apollo_subjects.search_space_id). 026 itself ALTERs apollo_problem_attempts
# (learner_update_pending), so `problem_attempts` is in the scope too — that
# pulls in 009 (which CREATEs apollo_sessions/apollo_problem_attempts that 026
# depends on), exactly mirroring the 026 harness's regex. The resolved chain is
# [009, 018, 025, 026]; 028 (which references 026's column) is excluded.
_TOUCHES_TARGETS = re.compile(
    r"(CREATE TABLE IF NOT EXISTS|CREATE TABLE|ALTER TABLE)\s+"
    r"apollo_(subjects|concepts|concept_problems|kg_entities|problem_attempts)\b"
)
_MIGRATION_030 = MIGRATIONS_DIR / "030_apollo_autoprovisioning.sql"
# 030 is applied LAST (separately), so it is excluded from the auto-built chain.
# 028 (WU-5B3a-0) ALTERs apollo_problem_attempts and so matches the regex, but its
# partial index predicate references `learner_update_pending` — a column 026
# creates — so it MUST be excluded: the chain applies 026 in filename order, and
# 028's forward dependency is its own concern (it has its own migration test).
# This mirrors the 026 harness's exclusion of 028 exactly.
_EXCLUDE_FROM_CHAIN = frozenset({
    _MIGRATION_030.name,
    "028_apollo_learner_janitor.sql",
})


def _chain_migrations() -> list[Path]:
    """Every migration defining/altering apollo_subjects/concepts/concept_problems/
    kg_entities, in filename order. Excludes 030 itself (applied separately last)
    and the defensive 028 guard (see _EXCLUDE_FROM_CHAIN)."""
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


async def _apply_chain(dsn: str, *, include_030: bool) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(_STUB_DDL)
        for migration in _chain_migrations():
            await conn.execute(migration.read_text(encoding="utf-8"))
        if include_030:
            await conn.execute(_MIGRATION_030.read_text(encoding="utf-8"))
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Chain-ordering proof (Risk MEDIUM mitigation): print + assert the resolved
# chain rather than assume it. 018 MUST precede 026 MUST precede 030, or the
# FK targets / the backfill-join column won't exist when 030 applies.
# ---------------------------------------------------------------------------


def test_chain_order_018_before_026_before_030():
    names = [p.name for p in _chain_migrations()]
    # Resolved chain (printed for the executor's one-time verification).
    print(f"\nResolved 030 migration chain: {names}")
    assert "009_apollo_slice0.sql" in names, names  # creates sessions/problem_attempts
    assert "018_apollo_concept_registry.sql" in names, names
    assert "026_apollo_learner_model.sql" in names, names
    assert "028_apollo_learner_janitor.sql" not in names, "028 depends on 026's column"
    assert _MIGRATION_030.name not in names, "030 must be applied separately, last"
    # Ordering invariant: 009 (base tables) < 018 (concepts) < 026 (kg_entities +
    # subjects.search_space_id) — or 030's FK targets / backfill-join won't exist.
    assert names.index("009_apollo_slice0.sql") < names.index(
        "018_apollo_concept_registry.sql"
    ), f"009 must precede 018: {names}"
    assert names.index("018_apollo_concept_registry.sql") < names.index(
        "026_apollo_learner_model.sql"
    ), f"018 must precede 026: {names}"


# ---------------------------------------------------------------------------
# Fully-migrated DB (chain + 030), shared across most tests via per-test
# transaction rollback. The backfill-specific tests build their own pre-030
# databases (a populated table BEFORE 030 is the whole point) below.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _migrated_dsn(_pg_url: str):
    base_db = make_url(_pg_url).database
    assert base_db, "container URL has no database name"
    admin_dsn = _plain_dsn(_pg_url, base_db)
    migrated_dsn = _plain_dsn(_pg_url, MIGRATED_DB_NAME)

    async def _setup() -> None:
        await _create_db(admin_dsn, MIGRATED_DB_NAME)
        await _apply_chain(migrated_dsn, include_030=True)

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
    ``mig_conn`` wraps each test in ONE transaction, so wrapping the violation in
    a SAVEPOINT (asyncpg nested transaction) and rolling back TO it clears the
    error state, letting the test keep issuing SQL on the same connection.
    (Copied verbatim from the 026 harness.)
    """
    sp = conn.transaction()
    await sp.start()
    try:
        with pytest.raises(error_type):
            await coro
    finally:
        await sp.rollback()


# ---- seed helpers (one course -> subject -> concept (-> concept_problem)) ----


async def _seed_space(conn: asyncpg.Connection) -> int:
    return await conn.fetchval(
        "INSERT INTO aita_search_spaces DEFAULT VALUES RETURNING id"
    )


async def _seed_subject(
    conn: asyncpg.Connection, space_id: int, slug: str = "s1"
) -> int:
    return await conn.fetchval(
        "INSERT INTO apollo_subjects (slug, display_name, search_space_id) "
        "VALUES ($1, 'Subj', $2) RETURNING id",
        slug,
        space_id,
    )


async def _seed_concept(
    conn: asyncpg.Connection, subject_id: int, slug: str = "c1"
) -> int:
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


async def _seed_concept_problem(
    conn: asyncpg.Connection,
    concept_id: int,
    *,
    problem_code: str = "p1",
    difficulty: str = "intro",
    tier: int | None = None,
    solution_source: str | None = None,
) -> int:
    """Insert one apollo_concept_problems row, return its id. ``tier`` /
    ``solution_source`` are omitted from the INSERT when None so the DB
    server_default is exercised."""
    # payload (position 4) is JSONB; bind the literal '{}' through an explicit cast.
    cols = ["concept_id", "problem_code", "difficulty", "payload"]
    placeholders = ["$1", "$2", "$3", "$4::jsonb"]
    vals: list = [concept_id, problem_code, difficulty, "{}"]
    if tier is not None:
        cols.append("tier")
        placeholders.append(f"${len(vals) + 1}")
        vals.append(tier)
    if solution_source is not None:
        cols.append("solution_source")
        placeholders.append(f"${len(vals) + 1}")
        vals.append(solution_source)
    sql = (
        f"INSERT INTO apollo_concept_problems ({', '.join(cols)}) "
        f"VALUES ({', '.join(placeholders)}) RETURNING id"
    )
    return await conn.fetchval(sql, *vals)


async def _seed_chain(conn: asyncpg.Connection, marker: int = 1):
    """Return (space_id, subject_id, concept_id) for one course. ``marker``
    disambiguates UNIQUE subject slugs so multiple chains coexist in one txn."""
    space = await _seed_space(conn)
    subject = await _seed_subject(conn, space, slug=f"s{marker}")
    concept = await _seed_concept(conn, subject, slug=f"c{marker}")
    return space, subject, concept


async def _seed_run(
    conn: asyncpg.Connection,
    space_id: int,
    *,
    document_id: int = 1,
    content_hash: str | None = None,
    status: str | None = None,
) -> int:
    cols = ["search_space_id", "document_id"]
    vals: list = [space_id, document_id]
    if content_hash is not None:
        cols.append("content_hash")
        vals.append(content_hash)
    if status is not None:
        cols.append("status")
        vals.append(status)
    placeholders = ", ".join(f"${i + 1}" for i in range(len(vals)))
    sql = (
        f"INSERT INTO apollo_ingest_runs ({', '.join(cols)}) "
        f"VALUES ({placeholders}) RETURNING id"
    )
    return await conn.fetchval(sql, *vals)


async def _seed_job(
    conn: asyncpg.Connection,
    space_id: int,
    *,
    document_id: int = 1,
    state: str = "pending",
    ingest_run_id: int | None = None,
) -> int:
    return await conn.fetchval(
        "INSERT INTO apollo_provisioning_jobs "
        "(search_space_id, document_id, state, ingest_run_id) "
        "VALUES ($1, $2, $3, $4) RETURNING id",
        space_id,
        document_id,
        state,
        ingest_run_id,
    )


# ===========================================================================
# Schema-shape tests
# ===========================================================================

_NEW_TABLES = (
    "apollo_ingest_runs",
    "apollo_provisioning_jobs",
    "apollo_rejected_problems",
    "apollo_dedup_decisions",
    "apollo_ingest_errors",
)


async def test_all_five_new_tables_created(mig_conn):
    """All five new tables exist after chain+030."""
    for table in _NEW_TABLES:
        regclass = await mig_conn.fetchval("SELECT to_regclass($1)", f"public.{table}")
        assert regclass is not None, f"{table} missing"


async def test_concept_problems_new_columns_exist(mig_conn):
    """The five new apollo_concept_problems columns exist with the expected data
    types and nullability."""
    rows = await mig_conn.fetch(
        "SELECT column_name, data_type, is_nullable "
        "FROM information_schema.columns "
        "WHERE table_name='apollo_concept_problems' "
        "  AND column_name IN "
        "  ('tier','solution_source','provenance','quarantined_at','search_space_id')"
    )
    by_name = {r["column_name"]: r for r in rows}
    assert set(by_name) == {
        "tier",
        "solution_source",
        "provenance",
        "quarantined_at",
        "search_space_id",
    }
    assert by_name["tier"]["data_type"] == "smallint"
    assert by_name["tier"]["is_nullable"] == "NO"
    assert by_name["solution_source"]["data_type"] == "text"
    assert by_name["solution_source"]["is_nullable"] == "YES"
    assert by_name["provenance"]["data_type"] == "jsonb"
    assert by_name["provenance"]["is_nullable"] == "NO"
    assert by_name["quarantined_at"]["data_type"] == "timestamp with time zone"
    assert by_name["quarantined_at"]["is_nullable"] == "YES"
    assert by_name["search_space_id"]["data_type"] == "integer"
    assert by_name["search_space_id"]["is_nullable"] == "YES"


async def test_kg_entities_scope_summary_exists(mig_conn):
    """scope_summary is a nullable text column on apollo_kg_entities."""
    row = await mig_conn.fetchrow(
        "SELECT data_type, is_nullable FROM information_schema.columns "
        "WHERE table_name='apollo_kg_entities' AND column_name='scope_summary'"
    )
    assert row is not None
    assert row["data_type"] == "text"
    assert row["is_nullable"] == "YES"


async def test_concept_tier_index_exists(mig_conn):
    """pg_indexes contains the (concept_id, tier) index."""
    idx = await mig_conn.fetchval(
        "SELECT indexname FROM pg_indexes "
        "WHERE tablename='apollo_concept_problems' "
        "  AND indexname='apollo_concept_problems_concept_tier_idx'"
    )
    assert idx == "apollo_concept_problems_concept_tier_idx"


async def test_ingest_runs_doc_hash_index_exists(mig_conn):
    """Both the content_hash short-circuit index and the (space, doc) index
    exist on apollo_ingest_runs."""
    names = {
        r["indexname"]
        for r in await mig_conn.fetch(
            "SELECT indexname FROM pg_indexes WHERE tablename='apollo_ingest_runs'"
        )
    }
    assert "apollo_ingest_runs_doc_hash_idx" in names
    assert "apollo_ingest_runs_space_doc_idx" in names


# ===========================================================================
# CHECK-constraint tests (positive + negative each)
# ===========================================================================


async def test_tier_check_accepts_1_and_2_rejects_others(mig_conn):
    _space, _subject, concept = await _seed_chain(mig_conn)
    await _seed_concept_problem(mig_conn, concept, problem_code="t1", tier=1)
    await _seed_concept_problem(mig_conn, concept, problem_code="t2", tier=2)
    await _expect_violation(
        mig_conn,
        asyncpg.CheckViolationError,
        _seed_concept_problem(mig_conn, concept, problem_code="t3", tier=3),
    )
    await _expect_violation(
        mig_conn,
        asyncpg.CheckViolationError,
        _seed_concept_problem(mig_conn, concept, problem_code="t0", tier=0),
    )


async def test_solution_source_check(mig_conn):
    _space, _subject, concept = await _seed_chain(mig_conn)
    # NULL ok (omit the column).
    await _seed_concept_problem(mig_conn, concept, problem_code="s_null")
    for i, src in enumerate(("extracted", "generated", "authored")):
        await _seed_concept_problem(
            mig_conn, concept, problem_code=f"s{i}", solution_source=src
        )
    await _expect_violation(
        mig_conn,
        asyncpg.CheckViolationError,
        _seed_concept_problem(
            mig_conn, concept, problem_code="s_bad", solution_source="invented"
        ),
    )


async def test_ingest_runs_status_check(mig_conn):
    space, _subject, _concept = await _seed_chain(mig_conn)
    for st in ("queued", "running", "succeeded", "failed"):
        await _seed_run(mig_conn, space, status=st)
    # 'pending' is the JOB vocabulary, NOT the run vocabulary.
    await _expect_violation(
        mig_conn,
        asyncpg.CheckViolationError,
        _seed_run(mig_conn, space, status="pending"),
    )


async def test_provisioning_jobs_state_check(mig_conn):
    space, _subject, _concept = await _seed_chain(mig_conn)
    for i, st in enumerate(("pending", "running", "completed", "failed")):
        await _seed_job(mig_conn, space, document_id=i, state=st)
    # 'queued' is the RUN vocabulary, NOT the job vocabulary.
    await _expect_violation(
        mig_conn,
        asyncpg.CheckViolationError,
        _seed_job(mig_conn, space, document_id=99, state="queued"),
    )


async def test_dedup_method_check(mig_conn):
    space, _subject, _concept = await _seed_chain(mig_conn)
    run = await _seed_run(mig_conn, space)
    for m in ("slug", "embedding", "llm_judge"):
        await mig_conn.execute(
            "INSERT INTO apollo_dedup_decisions "
            "(ingest_run_id, search_space_id, candidate_key, method, verdict) "
            "VALUES ($1, $2, 'k', $3, 'merged')",
            run,
            space,
            m,
        )
    await _expect_violation(
        mig_conn,
        asyncpg.CheckViolationError,
        mig_conn.execute(
            "INSERT INTO apollo_dedup_decisions "
            "(ingest_run_id, search_space_id, candidate_key, method, verdict) "
            "VALUES ($1, $2, 'k', 'fuzzy', 'merged')",
            run,
            space,
        ),
    )


async def test_dedup_verdict_check(mig_conn):
    space, _subject, _concept = await _seed_chain(mig_conn)
    run = await _seed_run(mig_conn, space)
    for v in ("merged", "distinct"):
        await mig_conn.execute(
            "INSERT INTO apollo_dedup_decisions "
            "(ingest_run_id, search_space_id, candidate_key, method, verdict) "
            "VALUES ($1, $2, 'k', 'slug', $3)",
            run,
            space,
            v,
        )
    await _expect_violation(
        mig_conn,
        asyncpg.CheckViolationError,
        mig_conn.execute(
            "INSERT INTO apollo_dedup_decisions "
            "(ingest_run_id, search_space_id, candidate_key, method, verdict) "
            "VALUES ($1, $2, 'k', 'slug', 'maybe')",
            run,
            space,
        ),
    )


# ===========================================================================
# Default-value tests
# ===========================================================================


async def test_concept_problem_tier_defaults_1(mig_conn):
    """Insert omitting tier -> row reads tier=1 (raw-DDL server_default). The ORM/
    seed-helper default is 2 (asserted in the ORM-parity file); this is the SQL
    default a raw/migration insert sees."""
    _space, _subject, concept = await _seed_chain(mig_conn)
    pid = await _seed_concept_problem(mig_conn, concept, problem_code="d1")
    tier = await mig_conn.fetchval(
        "SELECT tier FROM apollo_concept_problems WHERE id=$1", pid
    )
    assert tier == 1


async def test_ingest_runs_numeric_and_counter_defaults(mig_conn):
    space, _subject, _concept = await _seed_chain(mig_conn)
    run = await _seed_run(mig_conn, space)
    row = await mig_conn.fetchrow(
        "SELECT status, n_questions_scraped, n_promoted, n_rejected, n_dedup_merged, "
        "  llm_calls, llm_tokens_in, llm_tokens_out, llm_cost_usd, created_at "
        "FROM apollo_ingest_runs WHERE id=$1",
        run,
    )
    assert row["status"] == "queued"
    assert row["n_questions_scraped"] == 0
    assert row["n_promoted"] == 0
    assert row["n_rejected"] == 0
    assert row["n_dedup_merged"] == 0
    assert row["llm_calls"] == 0
    assert row["llm_tokens_in"] == 0
    assert row["llm_tokens_out"] == 0
    assert row["llm_cost_usd"] == 0
    assert row["created_at"] is not None


async def test_provenance_defaults_empty_jsonb(mig_conn):
    """Insert omitting provenance -> {}."""
    _space, _subject, concept = await _seed_chain(mig_conn)
    pid = await _seed_concept_problem(mig_conn, concept, problem_code="pv1")
    prov = await mig_conn.fetchval(
        "SELECT provenance FROM apollo_concept_problems WHERE id=$1", pid
    )
    assert prov == "{}"


# ===========================================================================
# RLS-enable tests (all five new tables, zero policies)
# ===========================================================================


@pytest.mark.parametrize("table", _NEW_TABLES)
async def test_rls_enabled_on_five_new_tables(mig_conn, table):
    relrowsecurity = await mig_conn.fetchval(
        "SELECT relrowsecurity FROM pg_class WHERE relname=$1", table
    )
    assert relrowsecurity is True, f"{table} should have RLS enabled"
    n_policies = await mig_conn.fetchval(
        "SELECT count(*) FROM pg_policies WHERE tablename=$1", table
    )
    assert n_policies == 0, f"{table} stopgap must declare ZERO policies"


# ===========================================================================
# Backfill tests (pre-030 populated DB)
# ===========================================================================


@pytest_asyncio.fixture
async def pre030_factory(_pg_url: str):
    """Build a fresh pre-030 DB (chain WITHOUT 030), let the test seed it, then
    apply 030. Yields an async builder; cleans up the databases afterwards."""
    base_db = make_url(_pg_url).database
    admin_dsn = _plain_dsn(_pg_url, base_db)
    created: list[str] = []

    async def _build(seed_coro):
        name = f"{PRE030_DB_PREFIX}_{len(created)}"
        created.append(name)
        dsn = _plain_dsn(_pg_url, name)
        await _create_db(admin_dsn, name)
        await _apply_chain(dsn, include_030=False)
        conn = await asyncpg.connect(dsn)
        try:
            await seed_coro(conn)
        finally:
            await conn.close()
        # Now apply 030 on the populated pre-030 DB.
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(_MIGRATION_030.read_text(encoding="utf-8"))
        finally:
            await conn.close()
        return dsn

    try:
        yield _build
    finally:
        admin = await asyncpg.connect(admin_dsn)
        try:
            for name in created:
                await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        finally:
            await admin.close()


async def test_backfill_b_flips_seeded_tier1_to_tier2_authored(pre030_factory):
    """MUTATION-PROVE: a pre-030 (018-shape, no tier column) concept_problem row
    is flipped to tier=2, solution_source='authored' by backfill (b). This is the
    bernoulli-pool-survival guarantee — without it the live teachable pool
    vanishes the instant the tier filter ships. Reverting the migration's
    'UPDATE ... SET tier=2' line makes this RED."""

    async def _seed(conn):
        space = await conn.fetchval(
            "INSERT INTO aita_search_spaces DEFAULT VALUES RETURNING id"
        )
        subject = await conn.fetchval(
            "INSERT INTO apollo_subjects (slug, display_name, search_space_id) "
            "VALUES ('s1', 'Subj', $1) RETURNING id",
            space,
        )
        concept = await conn.fetchval(
            "INSERT INTO apollo_concepts (subject_id, slug, display_name) "
            "VALUES ($1, 'c1', 'Concept') RETURNING id",
            subject,
        )
        # pre-030 apollo_concept_problems has NO tier/solution_source columns.
        await conn.execute(
            "INSERT INTO apollo_concept_problems "
            "(concept_id, problem_code, difficulty, payload) "
            "VALUES ($1, 'legacy', 'intro', '{}'::jsonb)",
            concept,
        )

    dsn = await pre030_factory(_seed)
    conn = await asyncpg.connect(dsn)
    try:
        row = await conn.fetchrow(
            "SELECT tier, solution_source FROM apollo_concept_problems "
            "WHERE problem_code='legacy'"
        )
        assert row["tier"] == 2, "seeded row must flip to tier=2 (teachable)"
        assert row["solution_source"] == "authored"
    finally:
        await conn.close()


async def test_backfill_a_denormalizes_search_space_id(pre030_factory):
    """backfill (a): search_space_id is populated via the concept->subject join
    and equals the seeded course id, for a row inserted pre-030."""

    async def _seed(conn):
        space = await conn.fetchval(
            "INSERT INTO aita_search_spaces DEFAULT VALUES RETURNING id"
        )
        subject = await conn.fetchval(
            "INSERT INTO apollo_subjects (slug, display_name, search_space_id) "
            "VALUES ('s1', 'Subj', $1) RETURNING id",
            space,
        )
        concept = await conn.fetchval(
            "INSERT INTO apollo_concepts (subject_id, slug, display_name) "
            "VALUES ($1, 'c1', 'Concept') RETURNING id",
            subject,
        )
        await conn.execute(
            "INSERT INTO apollo_concept_problems "
            "(concept_id, problem_code, difficulty, payload) "
            "VALUES ($1, 'p1', 'intro', '{}'::jsonb)",
            concept,
        )

    dsn = await pre030_factory(_seed)
    conn = await asyncpg.connect(dsn)
    try:
        ssid = await conn.fetchval(
            "SELECT search_space_id FROM apollo_concept_problems WHERE problem_code='p1'"
        )
        space_id = await conn.fetchval("SELECT MIN(id) FROM aita_search_spaces")
        assert ssid == space_id, "search_space_id must be denormalized from the join"
    finally:
        await conn.close()


async def test_backfill_a_noop_when_no_rows(pre030_factory):
    """Empty apollo_concept_problems pre-030: 030 applies cleanly, zero rows, the
    new columns exist. Proves the migration is safe on a fresh DB."""

    async def _seed(conn):  # nothing seeded
        return None

    dsn = await pre030_factory(_seed)
    conn = await asyncpg.connect(dsn)
    try:
        count = await conn.fetchval("SELECT count(*) FROM apollo_concept_problems")
        assert count == 0
        col = await conn.fetchval(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='apollo_concept_problems' AND column_name='tier'"
        )
        assert col == "tier"
    finally:
        await conn.close()


# ===========================================================================
# Partial-unique-index collapse tests (the idempotent-enqueue contract)
# ===========================================================================


async def test_open_job_partial_unique_blocks_second_open_job(mig_conn):
    """Two OPEN jobs for one document collapse: a second pending/running job for
    the same document_id raises UniqueViolationError."""
    space, _subject, _concept = await _seed_chain(mig_conn)
    await _seed_job(mig_conn, space, document_id=42, state="pending")
    await _expect_violation(
        mig_conn,
        asyncpg.UniqueViolationError,
        _seed_job(mig_conn, space, document_id=42, state="pending"),
    )
    # 'running' is also an OPEN state -> still collapses against the pending one.
    await _expect_violation(
        mig_conn,
        asyncpg.UniqueViolationError,
        _seed_job(mig_conn, space, document_id=42, state="running"),
    )


async def test_completed_plus_new_open_job_allowed(mig_conn):
    """The partial index only covers state IN ('pending','running'). A completed
    (or failed) job for D does NOT block a new pending job for the same D."""
    space, _subject, _concept = await _seed_chain(mig_conn)
    await _seed_job(mig_conn, space, document_id=7, state="completed")
    await _seed_job(mig_conn, space, document_id=7, state="pending")
    await _seed_job(mig_conn, space, document_id=8, state="failed")
    await _seed_job(mig_conn, space, document_id=8, state="pending")
    n = await mig_conn.fetchval(
        "SELECT count(*) FROM apollo_provisioning_jobs WHERE document_id IN (7, 8)"
    )
    assert n == 4


# ===========================================================================
# FK-cascade tests (course isolation + observability lifecycle)
# ===========================================================================


async def test_ingest_runs_course_cascade(mig_conn):
    space, _subject, _concept = await _seed_chain(mig_conn)
    await _seed_run(mig_conn, space)
    await mig_conn.execute("DELETE FROM aita_search_spaces WHERE id=$1", space)
    remaining = await mig_conn.fetchval(
        "SELECT count(*) FROM apollo_ingest_runs WHERE search_space_id=$1", space
    )
    assert remaining == 0


async def test_provisioning_jobs_ingest_run_set_null(mig_conn):
    space, _subject, _concept = await _seed_chain(mig_conn)
    run = await _seed_run(mig_conn, space)
    job = await _seed_job(mig_conn, space, document_id=3, ingest_run_id=run)
    await mig_conn.execute("DELETE FROM apollo_ingest_runs WHERE id=$1", run)
    row = await mig_conn.fetchrow(
        "SELECT ingest_run_id FROM apollo_provisioning_jobs WHERE id=$1", job
    )
    assert row is not None, "job must survive its ingest_run deletion"
    assert row["ingest_run_id"] is None


async def test_child_tables_ingest_run_cascade(mig_conn):
    """A rejected_problem / dedup_decision / ingest_error row is deleted when its
    ingest_run parent is deleted (ON DELETE CASCADE)."""
    space, _subject, concept = await _seed_chain(mig_conn)
    run = await _seed_run(mig_conn, space)
    await mig_conn.execute(
        "INSERT INTO apollo_rejected_problems "
        "(ingest_run_id, search_space_id, rejected_stage, diagnostic) "
        "VALUES ($1, $2, 'promotion_lint', 'bad')",
        run,
        space,
    )
    await mig_conn.execute(
        "INSERT INTO apollo_dedup_decisions "
        "(ingest_run_id, search_space_id, candidate_key, method, verdict) "
        "VALUES ($1, $2, 'k', 'slug', 'merged')",
        run,
        space,
    )
    await mig_conn.execute(
        "INSERT INTO apollo_ingest_errors "
        "(ingest_run_id, search_space_id, stage, error_class) "
        "VALUES ($1, $2, 'scrape', 'TimeoutError')",
        run,
        space,
    )
    await mig_conn.execute("DELETE FROM apollo_ingest_runs WHERE id=$1", run)
    for table in (
        "apollo_rejected_problems",
        "apollo_dedup_decisions",
        "apollo_ingest_errors",
    ):
        n = await mig_conn.fetchval(
            f"SELECT count(*) FROM {table} WHERE ingest_run_id=$1", run
        )
        assert n == 0, f"{table} row should cascade-delete with its ingest_run"


async def test_rejected_dedup_concept_set_null(mig_conn):
    """Deleting the referenced apollo_concepts row sets concept_id NULL on
    rejected/dedup rows (ON DELETE SET NULL); the rows survive."""
    space, _subject, concept = await _seed_chain(mig_conn)
    run = await _seed_run(mig_conn, space)
    rej = await mig_conn.fetchval(
        "INSERT INTO apollo_rejected_problems "
        "(ingest_run_id, search_space_id, concept_id, rejected_stage, diagnostic) "
        "VALUES ($1, $2, $3, 'pairing_gate', 'x') RETURNING id",
        run,
        space,
        concept,
    )
    ded = await mig_conn.fetchval(
        "INSERT INTO apollo_dedup_decisions "
        "(ingest_run_id, search_space_id, concept_id, candidate_key, method, verdict) "
        "VALUES ($1, $2, $3, 'k', 'slug', 'merged') RETURNING id",
        run,
        space,
        concept,
    )
    await mig_conn.execute("DELETE FROM apollo_concepts WHERE id=$1", concept)
    rej_cid = await mig_conn.fetchval(
        "SELECT concept_id FROM apollo_rejected_problems WHERE id=$1", rej
    )
    ded_cid = await mig_conn.fetchval(
        "SELECT concept_id FROM apollo_dedup_decisions WHERE id=$1", ded
    )
    assert rej_cid is None
    assert ded_cid is None


# ===========================================================================
# Access-control / isolation (positive + negative + cascade)
# ===========================================================================


async def _seed_two_courses_with_runs(conn: asyncpg.Connection):
    """Two independent courses, each with one ingest_run. Returns (sA, sB)."""
    sA, _subjA, _conA = await _seed_chain(conn, marker=10)
    await _seed_run(conn, sA, document_id=100)
    sB, _subjB, _conB = await _seed_chain(conn, marker=20)
    await _seed_run(conn, sB, document_id=200)
    return sA, sB


async def test_ingest_runs_scoped_select_returns_rows(mig_conn):
    sA, _sB = await _seed_two_courses_with_runs(mig_conn)
    rows = await mig_conn.fetch(
        "SELECT * FROM apollo_ingest_runs WHERE search_space_id=$1", sA
    )
    assert len(rows) == 1


async def test_ingest_runs_other_tenant_returns_zero_rows(mig_conn):
    sA, sB = await _seed_two_courses_with_runs(mig_conn)
    # Querying course-B's id returns ZERO of course-A's rows.
    rows = await mig_conn.fetch(
        "SELECT * FROM apollo_ingest_runs "
        "WHERE search_space_id=$1 AND document_id=$2",
        sB,
        100,
    )
    assert rows == []


async def test_course_delete_cascades_only_provisioning_rows(mig_conn):
    sA, sB = await _seed_two_courses_with_runs(mig_conn)
    # Add jobs to both courses too.
    await _seed_job(mig_conn, sA, document_id=100)
    await _seed_job(mig_conn, sB, document_id=200)
    await mig_conn.execute("DELETE FROM aita_search_spaces WHERE id=$1", sA)
    assert (
        await mig_conn.fetchval(
            "SELECT count(*) FROM apollo_ingest_runs WHERE search_space_id=$1", sA
        )
        == 0
    )
    assert (
        await mig_conn.fetchval(
            "SELECT count(*) FROM apollo_provisioning_jobs WHERE search_space_id=$1", sA
        )
        == 0
    )
    # course-B's rows are intact.
    assert (
        await mig_conn.fetchval(
            "SELECT count(*) FROM apollo_ingest_runs WHERE search_space_id=$1", sB
        )
        == 1
    )
    assert (
        await mig_conn.fetchval(
            "SELECT count(*) FROM apollo_provisioning_jobs WHERE search_space_id=$1", sB
        )
        == 1
    )
