"""Local-Docker contract tests for the DB-06 retrieval functions."""

from __future__ import annotations

import asyncio
import json
import os
import random
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
_RETRIEVAL = _MIGRATIONS / "20260717050000_retrieval_functions_v1.sql"
_DB_NAME = "retrieval_functions_v1"

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

# These are the reviewed live definitions captured in db-preflight.md. They
# deliberately retain the old unqualified search path so parity is measured
# against the actual legacy behavior, not another hardened implementation.
_LEGACY_FUNCTIONS = """
CREATE FUNCTION public.fetch_items(
    p_chunk_ids integer[], p_document_ids integer[]
) RETURNS SETOF public.aita_chunks
LANGUAGE sql STABLE AS $$
  SELECT * FROM aita_chunks
  WHERE id = ANY(p_chunk_ids) AND document_id = ANY(p_document_ids)
$$;

CREATE FUNCTION public.fts_count(
    query_text text, p_document_ids integer[]
) RETURNS integer
LANGUAGE sql STABLE AS $$
  SELECT COUNT(*)::INT FROM aita_chunks
  WHERE document_id = ANY(p_document_ids)
    AND to_tsvector('english', content)
        @@ websearch_to_tsquery('english', query_text)
$$;

CREATE FUNCTION public.hybrid_search(
    query_text text,
    query_embedding vector,
    p_document_ids integer[],
    match_count integer,
    rrf_k integer DEFAULT 60
) RETURNS TABLE(
    chunk_id int,
    document_id int,
    semantic_score float8,
    lexical_score float8,
    semantic_rank int,
    lexical_rank int
)
LANGUAGE sql STABLE AS $$
WITH sem AS (
  SELECT ac.id, ac.document_id,
    1.0 - (ac.embedding::halfvec(3072) <=> query_embedding::halfvec(3072)) AS score,
    ROW_NUMBER() OVER (
      ORDER BY ac.embedding::halfvec(3072) <=> query_embedding::halfvec(3072)
    )::INT AS rnk
  FROM aita_chunks ac
  ORDER BY ac.embedding::halfvec(3072) <=> query_embedding::halfvec(3072)
  LIMIT match_count * 10
),
sem_filtered AS (
  SELECT * FROM sem WHERE document_id = ANY(p_document_ids) LIMIT match_count * 2
),
lex AS (
  SELECT ac.id, ac.document_id,
    ts_rank_cd(
      to_tsvector('english', ac.content),
      websearch_to_tsquery('english', query_text)
    ) AS score,
    ROW_NUMBER() OVER (
      ORDER BY ts_rank_cd(
        to_tsvector('english', ac.content),
        websearch_to_tsquery('english', query_text)
      ) DESC
    )::INT AS rnk
  FROM aita_chunks ac
  WHERE ac.document_id = ANY(p_document_ids)
    AND to_tsvector('english', ac.content)
        @@ websearch_to_tsquery('english', query_text)
  LIMIT match_count * 2
)
SELECT COALESCE(s.id, l.id), COALESCE(s.document_id, l.document_id),
  COALESCE(s.score, 0.0)::FLOAT, COALESCE(l.score, 0.0)::FLOAT,
  COALESCE(s.rnk, match_count * 2 + 1)::INT,
  COALESCE(l.rnk, match_count * 2 + 1)::INT
FROM sem_filtered s
FULL OUTER JOIN lex l ON s.id = l.id AND s.document_id = l.document_id
ORDER BY (
  1.0/(rrf_k + COALESCE(s.rnk, match_count*2+1))
  + 1.0/(rrf_k + COALESCE(l.rnk, match_count*2+1))
) DESC
LIMIT match_count
$$;
"""


def _dsn(url: str, database: str) -> str:
    return (
        make_url(url)
        .set(drivername="postgresql", database=database)
        .render_as_string(hide_password=False)
    )


def _vector(seed: int) -> str:
    rng = random.Random(seed)
    return "[" + ",".join(f"{rng.uniform(-1, 1):.8f}" for _ in range(3072)) + "]"


async def _seed_corpus(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        INSERT INTO public.aita_search_spaces
          (id, name, slug, subject_name, weight_overrides, metadata)
        VALUES (1, 'Retrieval course', 'retrieval-course', 'Physics', '{}', '{}');

        INSERT INTO public.aita_documents
          (id, title, material_kind, content, content_hash, document_metadata,
           page_count, status, search_space_id)
        VALUES
          (10, 'Fluid notes', 'notes', 'fluid mechanics notes', 'db06-doc-10',
           '{}', 6, '{"state":"ready"}', 1),
          (20, 'Optics notes', 'notes', 'optics notes', 'db06-doc-20',
           '{}', 6, '{"state":"ready"}', 1);
        """
    )
    texts = (
        "bernoulli pressure velocity conservation",
        "continuity equation mass flow velocity",
        "normal shock pressure ratio mach number",
        "boundary layer viscous shear stress",
        "compressible nozzle stagnation pressure",
        "hydrostatic pressure depth density",
        "geometric optics focal length lens",
        "diffraction wavelength aperture pattern",
        "reflection angle mirror ray",
        "refraction snell law index",
        "polarization electric field direction",
        "interference phase coherent sources",
    )
    for offset, content in enumerate(texts, start=1):
        document_id = 10 if offset <= 6 else 20
        await conn.execute(
            """
            INSERT INTO public.aita_chunks
              (id, content, embedding, page_number, section_path, chunk_type,
               figure_id, document_id, created_at)
            VALUES ($1, $2, $3::vector, $4, $5, 'body', NULL, $6,
                    '2026-07-17T00:00:00Z'::timestamptz)
            """,
            100 + offset,
            content,
            _vector(offset),
            offset if offset <= 6 else offset - 6,
            f"section-{offset}",
            document_id,
        )


@pytest.fixture(scope="session")
def retrieval_schema_dsn(request: pytest.FixtureRequest):
    local_override = os.getenv("DB06_LOCAL_TEST_DATABASE_URL")
    pg_url = local_override or request.getfixturevalue("_pg_url")
    parsed_url = make_url(pg_url)
    if local_override:
        assert parsed_url.host in {"127.0.0.1", "localhost", "::1"}, (
            "DB06_LOCAL_TEST_DATABASE_URL must target loopback"
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
            await conn.execute(_LEGACY_FUNCTIONS)
            await _seed_corpus(conn)
            await conn.execute(_CREATE.read_text(encoding="utf-8"))
            await conn.execute(_COPY.read_text(encoding="utf-8"))
            await conn.execute(_RETRIEVAL.read_text(encoding="utf-8"))
            await conn.execute("ANALYZE public.aita_chunks")
            await conn.execute("ANALYZE internal.document_chunks")
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
async def retrieval_conn(retrieval_schema_dsn: str):
    conn = await asyncpg.connect(retrieval_schema_dsn)
    try:
        yield conn
    finally:
        await conn.close()


def _rows(records: list[asyncpg.Record]) -> list[tuple[object, ...]]:
    return [tuple(record.values()) for record in records]


@pytest.mark.asyncio
async def test_old_new_function_parity(retrieval_conn: asyncpg.Connection) -> None:
    chunk_ids = [101, 103, 107, 112]
    document_ids = [10, 20]
    old_items = await retrieval_conn.fetch(
        "SELECT * FROM public.fetch_items($1::integer[], $2::integer[]) ORDER BY id",
        chunk_ids,
        document_ids,
    )
    new_items = await retrieval_conn.fetch(
        "SELECT * FROM internal.fetch_items($1::integer[], $2::integer[]) ORDER BY id",
        chunk_ids,
        document_ids,
    )
    assert _rows(new_items) == _rows(old_items)

    old_count = await retrieval_conn.fetchval(
        "SELECT public.fts_count('pressure velocity', $1::integer[])", [10]
    )
    new_count = await retrieval_conn.fetchval(
        "SELECT internal.fts_count('pressure velocity', $1::integer[])", [10]
    )
    assert new_count == old_count

    query_vector = _vector(3)
    old_search = await retrieval_conn.fetch(
        """
        SELECT * FROM public.hybrid_search(
          'pressure velocity', $1::vector, $2::integer[], 8, 60
        )
        """,
        query_vector,
        document_ids,
    )
    new_search = await retrieval_conn.fetch(
        """
        SELECT * FROM internal.hybrid_search(
          'pressure velocity', $1::extensions.vector, $2::integer[], 8, 60
        )
        """,
        query_vector,
        document_ids,
    )
    assert len(new_search) == len(old_search)
    for old, new in zip(old_search, new_search, strict=True):
        assert new["chunk_id"] == old["chunk_id"]
        assert new["document_id"] == old["document_id"]
        assert new["semantic_rank"] == old["semantic_rank"]
        assert new["lexical_rank"] == old["lexical_rank"]
        assert new["semantic_score"] == pytest.approx(old["semantic_score"], abs=1e-12)
        assert new["lexical_score"] == pytest.approx(old["lexical_score"], abs=1e-12)


@pytest.mark.asyncio
async def test_empty_search_path_defeats_hostile_decoys(
    retrieval_conn: asyncpg.Connection,
) -> None:
    await retrieval_conn.execute(
        """
        CREATE SCHEMA hostile;
        CREATE TABLE hostile.aita_chunks (
          id integer, content text, document_id integer
        );
        CREATE TABLE hostile.document_chunks (
          id bigint, content text, document_id bigint
        );
        INSERT INTO hostile.aita_chunks VALUES (999, 'pressure velocity', 10);
        INSERT INTO hostile.document_chunks VALUES (999, 'pressure velocity', 10);
        """
    )
    baseline = await retrieval_conn.fetchval(
        "SELECT internal.fts_count('pressure velocity', ARRAY[10]::integer[])"
    )
    transaction = retrieval_conn.transaction()
    await transaction.start()
    try:
        await retrieval_conn.execute("SET LOCAL search_path = hostile, public")
        hostile_result = await retrieval_conn.fetchval(
            "SELECT internal.fts_count('pressure velocity', ARRAY[10]::integer[])"
        )
    finally:
        await transaction.rollback()
    assert hostile_result == baseline

    configs = await retrieval_conn.fetch(
        """
        SELECT p.proname, p.proconfig
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'internal'
          AND p.proname IN ('fetch_items', 'fts_count', 'hybrid_search')
          AND pg_get_function_identity_arguments(p.oid) LIKE '%integer%'
        ORDER BY p.proname
        """
    )
    assert len(configs) == 3
    assert all(
        any(setting.startswith("search_path=") for setting in record["proconfig"])
        for record in configs
    )


@pytest.mark.asyncio
async def test_service_role_is_only_rpc_grantee(
    retrieval_conn: asyncpg.Connection,
) -> None:
    privileges = await retrieval_conn.fetch(
        """
        SELECT privilege.routine_name, privilege.grantee, owner.rolname AS owner_name
        FROM information_schema.routine_privileges
        AS privilege
        JOIN pg_proc AS routine ON routine.proname = privilege.routine_name
        JOIN pg_namespace AS namespace
          ON namespace.oid = routine.pronamespace
         AND namespace.nspname = privilege.specific_schema
        JOIN pg_roles AS owner ON owner.oid = routine.proowner
        WHERE specific_schema = 'internal'
          AND routine_name IN ('fetch_items', 'fts_count', 'hybrid_search')
          AND privilege_type = 'EXECUTE'
          AND pg_get_function_identity_arguments(routine.oid) LIKE '%integer%'
        ORDER BY routine_name, grantee
        """
    )
    by_function: dict[str, set[str]] = {}
    owners: dict[str, str] = {}
    for privilege in privileges:
        by_function.setdefault(privilege["routine_name"], set()).add(privilege["grantee"])
        owners[privilege["routine_name"]] = privilege["owner_name"]
    for function_name in ("fetch_items", "fts_count", "hybrid_search"):
        assert by_function[function_name] == {owners[function_name], "service_role"}

    transaction = retrieval_conn.transaction()
    await transaction.start()
    try:
        await retrieval_conn.execute("SET LOCAL ROLE service_role")
        assert await retrieval_conn.fetchval(
            "SELECT internal.fts_count('pressure', ARRAY[10]::integer[])"
        ) == 4
    finally:
        await transaction.rollback()


@pytest.mark.asyncio
async def test_hnsw_index_is_valid_and_used(
    retrieval_conn: asyncpg.Connection,
) -> None:
    index_row = await retrieval_conn.fetchrow(
        """
        SELECT i.indisvalid, pg_get_indexdef(i.indexrelid) AS definition
        FROM pg_index i
        WHERE i.indexrelid =
          'internal.document_chunks__embedding_halfvec_hnsw__idx'::regclass
        """
    )
    assert index_row is not None
    assert index_row["indisvalid"] is True
    assert "USING hnsw" in index_row["definition"]
    assert "extensions.halfvec_cosine_ops" in index_row["definition"]

    transaction = retrieval_conn.transaction()
    await transaction.start()
    try:
        await retrieval_conn.execute("SET LOCAL enable_seqscan = off")
        plan_value = await retrieval_conn.fetchval(
            """
            EXPLAIN (FORMAT JSON, COSTS OFF)
            SELECT id
            FROM internal.document_chunks
            ORDER BY embedding::extensions.halfvec(3072)
              OPERATOR(extensions.<=>)
              ($1::extensions.vector)::extensions.halfvec(3072)
            LIMIT 5
            """,
            _vector(3),
        )
    finally:
        await transaction.rollback()
    plan = json.dumps(plan_value) if not isinstance(plan_value, str) else plan_value
    assert "document_chunks__embedding_halfvec_hnsw__idx" in plan
    assert "Index Scan" in plan


def test_migration_reuses_the_db04_retrieval_index_allowlist() -> None:
    sql = _RETRIEVAL.read_text(encoding="utf-8")
    executable = sql.split("SET lock_timeout", 1)[1].lower()
    assert "create index" not in executable
    assert "create extension if not exists vector with schema extensions" in executable
    assert "alter extension vector set schema extensions" not in executable
    for index_name in (
        "document_chunks__document_page__idx",
        "document_chunks__content_fts__idx",
        "document_chunks__embedding_halfvec_hnsw__idx",
    ):
        assert index_name in executable

    comment = sql.split("SET lock_timeout", 1)[0].lower()
    assert "human-only live relocation gate" in comment
    assert "abort the extension move" in comment
    assert "alter extension vector set schema extensions" in comment
