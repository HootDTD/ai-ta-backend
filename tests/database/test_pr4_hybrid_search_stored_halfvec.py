"""PR-4 real-Postgres tests: stored generated ``embedding_halfvec`` column.

Applies the full migration chain through the PR-4 migration
(20260731130000) on a fresh Testcontainers pgvector database, seeds a
~1.5k-row chunk corpus with realistic TOAST-sized 3072-dim embeddings, and
verifies:

1. Structure: the old expression index is gone, the new stored-column HNSW
   index exists, and the generated column's values match a manual cast.
2. Result parity: the semantic arm's OLD per-row-cast query shape and the NEW
   stored-column query shape return candidates in IDENTICAL rank order on the
   same corpus — the rewrite must not change which chunks win or their order.
3. The final hybrid_search() query never selects either embedding column
   (PR-4 (b)(1) — the pure detoast-waste fix).
4. EXPLAIN (ANALYZE, BUFFERS) plan shape for both query forms as app_runtime,
   printed for the perf record (run with -s to see the numbers).
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import asyncpg
import numpy as np
import pytest
import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

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
_PR4 = _MIGRATIONS / "20260731130000_pr4_hybrid_search_stored_halfvec.sql"
_DB_NAME = "pr4_hybrid_search_stored_halfvec"

TEACHER = "e1000000-0000-4000-8000-000000000001"
DIM = 3072
N_CHUNKS = 1500
N_NOISE_BATCHES = 4
N_NOISE_PER_BATCH = 1500  # 6,000 rows from other courses -- see _seed's noise comment
NEEDLE_TEXT = "the bernoulli equation relates pressure velocity and elevation in fluid flow"

# Same Supabase Auth role/function shim used by the other real-Postgres
# migration tests (see tests/database/test_db08d_rls_inline_membership_policies.py).
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

_OLD_SEMANTIC_SQL = """
    WITH semantic_candidates AS (
        SELECT id,
               (embedding::extensions.halfvec(3072) OPERATOR(extensions.<=>) $1::extensions.halfvec(3072)) AS distance
        FROM internal.document_chunks
        WHERE document_id = ANY($2::integer[])
        ORDER BY embedding::extensions.halfvec(3072) OPERATOR(extensions.<=>) $1::extensions.halfvec(3072)
        LIMIT $3
    )
    SELECT id, rank() OVER (ORDER BY distance) AS rnk
    FROM semantic_candidates
    ORDER BY rnk
"""

_NEW_SEMANTIC_SQL = """
    WITH semantic_candidates AS (
        SELECT id,
               (embedding_halfvec OPERATOR(extensions.<=>) $1::extensions.halfvec(3072)) AS distance
        FROM internal.document_chunks
        WHERE document_id = ANY($2::integer[])
        ORDER BY embedding_halfvec OPERATOR(extensions.<=>) $1::extensions.halfvec(3072)
        LIMIT $3
    )
    SELECT id, rank() OVER (ORDER BY distance) AS rnk
    FROM semantic_candidates
    ORDER BY rnk
"""


def _fmt(vec: np.ndarray) -> str:
    return "[" + ",".join(f"{x:.8f}" for x in vec.tolist()) + "]"


def _dsn(url: str, database: str) -> str:
    return (
        make_url(url)
        .set(drivername="postgresql", database=database)
        .render_as_string(hide_password=False)
    )


async def _seed(conn: asyncpg.Connection) -> dict:
    rng = np.random.default_rng(20260731)

    await conn.execute("INSERT INTO auth.users (id) VALUES ($1)", TEACHER)
    course_id = await conn.fetchval(
        "INSERT INTO app.courses (name, slug, subject_name) "
        "VALUES ('PR4 Course', 'pr4-course', 'Physics') RETURNING id"
    )
    await conn.execute(
        "INSERT INTO app.course_memberships (user_id, course_id, role) VALUES ($1, $2, 'teacher')",
        TEACHER,
        course_id,
    )

    doc_ids = []
    for i in range(2):
        doc_id = await conn.fetchval(
            "INSERT INTO app.documents "
            "(course_id, title, content, content_hash, material_kind, status) "
            "VALUES ($1, $2, 'body', $3, 'other', 'ready') RETURNING id",
            course_id,
            f"pr4-doc-{i}",
            f"pr4-hash-{i}",
        )
        doc_ids.append(doc_id)

    needle_vec = rng.normal(size=DIM)
    needle_vec /= np.linalg.norm(needle_vec)

    rows = [(course_id, doc_ids[0], NEEDLE_TEXT, _fmt(needle_vec), 1, "body")]
    for i in range(N_CHUNKS - 1):
        vec = rng.normal(size=DIM)
        vec /= np.linalg.norm(vec)
        content = f"generic filler chunk number {i} about thermodynamics and heat transfer"
        rows.append((course_id, doc_ids[i % 2], content, _fmt(vec), i + 2, "body"))

    await conn.executemany(
        "INSERT INTO internal.document_chunks "
        "(course_id, document_id, content, embedding, page_number, chunk_type) "
        "VALUES ($1, $2, $3, $4::extensions.vector, $5, $6)",
        rows,
    )

    # Noise from OTHER courses so internal.document_chunks isn't just our
    # target course's rows -- production has many courses in one table, and
    # the document_id = ANY(:visible_ids) filter's SELECTIVITY (not the
    # target course's row count alone) is what makes the planner's index
    # choice representative. Without this, the filter matches ~100% of the
    # table and a Bitmap Heap Scan always wins trivially either way.
    for batch in range(N_NOISE_BATCHES):
        noise_course_id = await conn.fetchval(
            "INSERT INTO app.courses (name, slug, subject_name) VALUES ($1, $2, 'Physics') RETURNING id",
            f"PR4 Noise Course {batch}",
            f"pr4-noise-course-{batch}",
        )
        noise_doc_id = await conn.fetchval(
            "INSERT INTO app.documents "
            "(course_id, title, content, content_hash, material_kind, status) "
            "VALUES ($1, $2, 'body', $3, 'other', 'ready') RETURNING id",
            noise_course_id,
            f"pr4-noise-doc-{batch}",
            f"pr4-noise-hash-{batch}",
        )
        noise_rows = []
        for i in range(N_NOISE_PER_BATCH):
            vec = rng.normal(size=DIM)
            vec /= np.linalg.norm(vec)
            content = f"noise course {batch} chunk {i} about unrelated economics history"
            noise_rows.append((noise_course_id, noise_doc_id, content, _fmt(vec), i + 1, "body"))
        await conn.executemany(
            "INSERT INTO internal.document_chunks "
            "(course_id, document_id, content, embedding, page_number, chunk_type) "
            "VALUES ($1, $2, $3, $4::extensions.vector, $5, $6)",
            noise_rows,
        )

    needle_id = await conn.fetchval(
        "SELECT id FROM internal.document_chunks WHERE content = $1", NEEDLE_TEXT
    )
    total_chunks = await conn.fetchval("SELECT count(*) FROM internal.document_chunks")

    return {
        "course_id": course_id,
        "doc_ids": doc_ids,
        "needle_vec": needle_vec,
        "needle_id": needle_id,
        "total_chunks": total_chunks,
    }


@pytest.fixture(scope="session")
def pr4_env(request: pytest.FixtureRequest) -> dict:
    """Fresh DB with the full chain through PR-4 applied, plus seed data."""
    pg_url = request.getfixturevalue("_pg_url")
    base_database = make_url(pg_url).database
    assert base_database
    admin_dsn = _dsn(pg_url, base_database)
    test_dsn = _dsn(pg_url, _DB_NAME)

    async def setup() -> dict:
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
                _PR4,
            ):
                await conn.execute(migration.read_text(encoding="utf-8"))
            # Real Supabase projects grant USAGE on `extensions` broadly at the
            # platform level (outside these migrations) -- replicate that here
            # so app_runtime can reference extensions.halfvec in its queries,
            # same as it does against the real database.
            await conn.execute("GRANT USAGE ON SCHEMA extensions TO authenticated, app_runtime")
            seeded = await _seed(conn)
        finally:
            await conn.close()
        return {"dsn": test_dsn, **seeded}

    async def teardown() -> None:
        admin = await asyncpg.connect(admin_dsn)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{_DB_NAME}" WITH (FORCE)')
        finally:
            await admin.close()

    env = asyncio.run(setup())
    try:
        yield env
    finally:
        asyncio.run(teardown())


@pytest.fixture(scope="session")
def seeded(pr4_env: dict) -> dict:
    return pr4_env


@pytest_asyncio.fixture
async def conn(pr4_env: dict):
    connection = await asyncpg.connect(pr4_env["dsn"])
    try:
        yield connection
    finally:
        await connection.close()


@pytest_asyncio.fixture
async def orm_session(pr4_env: dict):
    url = (
        make_url(pr4_env["dsn"])
        .set(drivername="postgresql+asyncpg")
        .render_as_string(hide_password=False)
    )
    engine = create_async_engine(
        url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": "public,extensions"}},
    )
    conn = await engine.connect()
    trans = await conn.begin()
    session = AsyncSession(
        bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    try:
        yield session
    finally:
        await session.close()
        if trans.is_active:
            await trans.rollback()
        await conn.close()
        await engine.dispose()


# ===========================================================================
# 1. Structure
# ===========================================================================


async def test_old_index_dropped_new_index_present(conn):
    old = await conn.fetchval(
        "SELECT to_regclass('internal.document_chunks__embedding_halfvec_hnsw__idx')"
    )
    assert old is None, "old expression index must be dropped by the PR-4 migration"
    new = await conn.fetchval(
        "SELECT to_regclass('internal.document_chunks__embedding_halfvec_stored_hnsw__idx')"
    )
    assert new is not None, "new stored-column HNSW index must exist"


async def test_generated_column_matches_manual_cast(conn):
    # halfvec has no equality operator; compare via its (deterministic) text
    # representation instead.
    mismatches = await conn.fetchval(
        "SELECT count(*) FROM internal.document_chunks "
        "WHERE embedding_halfvec::text IS DISTINCT FROM ((embedding)::extensions.halfvec(3072))::text"
    )
    assert mismatches == 0


async def test_migration_is_rerunnable(conn):
    before_cols = await conn.fetch(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'internal' AND table_name = 'document_chunks' ORDER BY column_name"
    )
    await conn.execute(_PR4.read_text(encoding="utf-8"))
    after_cols = await conn.fetch(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'internal' AND table_name = 'document_chunks' ORDER BY column_name"
    )
    assert [dict(r) for r in before_cols] == [dict(r) for r in after_cols]
    new = await conn.fetchval(
        "SELECT to_regclass('internal.document_chunks__embedding_halfvec_stored_hnsw__idx')"
    )
    assert new is not None


# ===========================================================================
# 2. Result parity: OLD per-row-cast query shape vs NEW stored-column shape.
# ===========================================================================


async def test_semantic_ranking_parity_old_vs_new_query_shape(conn, seeded):
    doc_ids = seeded["doc_ids"]
    query_vec = _fmt(seeded["needle_vec"])

    tx = conn.transaction()
    await tx.start()
    try:
        await conn.execute("SET LOCAL ROLE app_runtime")
        old_rows = await conn.fetch(_OLD_SEMANTIC_SQL, query_vec, doc_ids, 300)
        new_rows = await conn.fetch(_NEW_SEMANTIC_SQL, query_vec, doc_ids, 300)
    finally:
        await tx.rollback()

    old_order = [r["id"] for r in old_rows]
    new_order = [r["id"] for r in new_rows]
    assert len(old_order) == 300  # inner subquery LIMIT
    assert old_order == new_order, "the rewrite must not reorder semantic candidates"
    assert old_order[0] == seeded["needle_id"], "the needle must rank first for its own vector"


async def test_semantic_ranking_parity_random_query_vectors(conn, seeded):
    """Same parity check with query vectors that are NOT in the corpus, so the
    comparison isn't just validating the trivial distance-zero case."""
    doc_ids = seeded["doc_ids"]
    rng = np.random.default_rng(7)

    tx = conn.transaction()
    await tx.start()
    try:
        await conn.execute("SET LOCAL ROLE app_runtime")
        for _ in range(5):
            vec = rng.normal(size=DIM)
            vec /= np.linalg.norm(vec)
            query_vec = _fmt(vec)
            old_rows = await conn.fetch(_OLD_SEMANTIC_SQL, query_vec, doc_ids, 60)
            new_rows = await conn.fetch(_NEW_SEMANTIC_SQL, query_vec, doc_ids, 60)
            assert [r["id"] for r in old_rows] == [r["id"] for r in new_rows]
    finally:
        await tx.rollback()


# ===========================================================================
# 3. The final hybrid_search() query never selects an embedding column.
# ===========================================================================


async def test_hybrid_search_end_to_end_omits_embedding_columns(orm_session, seeded, monkeypatch):
    from retrieval import hybrid_search as hybrid_search_module
    from retrieval.hybrid_search import AITAHybridSearchRetriever

    needle_vec = seeded["needle_vec"]
    monkeypatch.setattr(hybrid_search_module, "embed_text", lambda _q: needle_vec.tolist())

    await orm_session.execute(text("SET LOCAL ROLE app_runtime"))
    await orm_session.execute(
        text("SELECT set_config('request.jwt.claims', :claims, true)"),
        {"claims": f'{{"sub":"{TEACHER}","role":"authenticated"}}'},
    )

    # Column identity, not SQL text, is what actually matters: capture the
    # real cursor.description of every executed statement and assert none of
    # them RETURNS an embedding column. (The semantic CTE legitimately
    # *references* embedding_halfvec in its distance expression — that's a
    # WHERE/ORDER BY usage, not a returned column, so text-matching the SQL
    # string would be fragile; the wire-level column list is unambiguous.)
    returned_columns: list[tuple[str, ...]] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        if cursor.description:
            returned_columns.append(tuple(d[0] for d in cursor.description))

    sync_engine = orm_session.bind.sync_engine
    event.listen(sync_engine, "after_cursor_execute", _capture)
    try:
        retriever = AITAHybridSearchRetriever(orm_session, search_space_id=seeded["course_id"])
        results = await retriever.hybrid_search(NEEDLE_TEXT, top_k=20)
    finally:
        event.remove(sync_engine, "after_cursor_execute", _capture)

    assert results, "expected the needle chunk to come back"
    assert results[0]["chunk_id"] == seeded["needle_id"]
    assert "embedding" not in results[0]

    assert returned_columns, "expected to capture at least one executed statement"
    for columns in returned_columns:
        embedding_cols = [c for c in columns if c.startswith("embedding")]
        assert not embedding_cols, f"an embedding column was returned on the wire: {embedding_cols}"


# ===========================================================================
# 4. EXPLAIN (ANALYZE, BUFFERS): plan shape + timing record for the PR-4 report.
# Run with `pytest -s -k test_explain_analyze_plan_shapes_and_timing` to see
# the printed numbers.
# ===========================================================================


async def _explain(conn: asyncpg.Connection, sql: str, *args) -> dict:
    rows = await conn.fetch(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {sql}", *args)
    return json.loads(rows[0][0])[0]


def _plan_text(plan: dict, depth: int = 0) -> str:
    node_type = plan.get("Node Type", "?")
    extra = ""
    if "Index Name" in plan:
        extra = f" using {plan['Index Name']}"
    elif "Relation Name" in plan:
        extra = f" on {plan['Relation Name']}"
    line = (
        f"{'  ' * depth}{node_type}{extra} "
        f"(actual time={plan.get('Actual Total Time')} rows={plan.get('Actual Rows')} "
        f"shared_hit={plan.get('Shared Hit Blocks')} shared_read={plan.get('Shared Read Blocks')})"
    )
    children = plan.get("Plans", [])
    return "\n".join([line] + [_plan_text(child, depth + 1) for child in children])


def _uses_index(plan: dict, index_name: str) -> bool:
    if plan.get("Index Name") == index_name:
        return True
    return any(_uses_index(child, index_name) for child in plan.get("Plans", []))


async def test_explain_analyze_plan_shapes_and_timing(conn, seeded):
    doc_ids = seeded["doc_ids"]
    query_vec = _fmt(seeded["needle_vec"])

    tx = conn.transaction()
    await tx.start()
    try:
        await conn.execute("SET LOCAL ROLE app_runtime")
        # Same iterative-scan GUCs the live retriever sets before its fused
        # query (retrieval/hybrid_search.py::_iterative_scan_statements) —
        # isolates the stored-column change as the only variable. They are a
        # no-op for the OLD query, which has no HNSW index left to iterate.
        await conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
        await conn.execute("SET LOCAL hnsw.ef_search = 300")
        await conn.execute("SET LOCAL hnsw.max_scan_tuples = 20000")

        t0 = time.perf_counter()
        old_plan = await _explain(conn, _OLD_SEMANTIC_SQL, query_vec, doc_ids, 300)
        old_wall = time.perf_counter() - t0

        t0 = time.perf_counter()
        new_plan = await _explain(conn, _NEW_SEMANTIC_SQL, query_vec, doc_ids, 300)
        new_wall = time.perf_counter() - t0
    finally:
        await tx.rollback()

    old_root = old_plan["Plan"]
    new_root = new_plan["Plan"]

    print(
        f"\n[PR-4] corpus={seeded['total_chunks']} total chunks, "
        f"{N_CHUNKS} in the {len(doc_ids)}-document filtered course"
    )
    print(f"[PR-4] OLD (per-row cast on `embedding`) wall={old_wall * 1000:.1f}ms")
    print(f"[PR-4]   pg execution time={old_plan.get('Execution Time')}ms")
    print(_plan_text(old_root))
    print(f"[PR-4] NEW (stored `embedding_halfvec` column) wall={new_wall * 1000:.1f}ms")
    print(f"[PR-4]   pg execution time={new_plan.get('Execution Time')}ms")
    print(_plan_text(new_root))

    # The old query shape can no longer use ANY hnsw index — its expression
    # index was dropped in the same migration that added the new column.
    assert not _uses_index(old_root, "document_chunks__embedding_halfvec_hnsw__idx")

    # At this corpus size (~7.5k rows, ~20% selectivity) the planner's own
    # cost model chooses a Seq Scan for BOTH forms -- same as the 0-scans
    # finding in staging pg_stat_user_indexes this PR starts from, and
    # expected: at small/medium scale a full scan genuinely can beat an ANN
    # index scan on cost. So this is not a "does it hit the HNSW index"
    # assertion (see test_old_index_dropped_new_index_present for that
    # structural check) -- it is the actual claim of PR-4 (b)(2): even under
    # an IDENTICAL Seq Scan plan shape, reading the pre-computed
    # embedding_halfvec column outperforms casting `embedding` per row on
    # every scanned tuple, because the CAST computation itself -- not index
    # usage -- was the dominant cost.
    old_ms = old_plan["Execution Time"]
    new_ms = new_plan["Execution Time"]
    assert new_ms < old_ms * 0.5, (
        f"expected the stored-column form to meaningfully beat the per-row-cast "
        f"form on execution time alone: old={old_ms}ms new={new_ms}ms"
    )
