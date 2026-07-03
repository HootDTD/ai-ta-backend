"""Real-Postgres tests for migration 036 (WU-AAS ingestion observability).

``036_apollo_ingest_page_evidence.sql`` extends the migration-030 observability
substrate: it adds ``apollo_ingest_runs.n_pages`` and creates
``apollo_ingest_page_evidence`` (per-page OCR text + confidence + verify-path
flag). Both are what SQLite ``create_all`` cannot prove (guarded ADD COLUMN,
server defaults, the FK CASCADE from the run, RLS), so these tests apply the
ACTUAL apollo chain (the same [009, 018, 025, 026] + 030 chain the 030 harness
uses) plus 036 to a fresh DB on the session pgvector container via raw asyncpg.

These MUST RUN GREEN (not skip) with Docker up — a skip is a FAIL of this gate.
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
MIGRATED_DB_NAME = "apollo_ingest_page_evidence_migrations"

# FK targets no on-disk migration creates (Supabase-managed / app-metadata).
_STUB_DDL = """
CREATE SCHEMA IF NOT EXISTS auth;
CREATE TABLE IF NOT EXISTS auth.users (id UUID PRIMARY KEY);
CREATE TABLE IF NOT EXISTS aita_search_spaces (id SERIAL PRIMARY KEY);
"""

# The pre-030 apollo chain that 030's ALTER/FK targets need (mirrors the 030
# harness): 009/018/025/026 create apollo_subjects/concepts/concept_problems/
# kg_entities/problem_attempts.
_TOUCHES_TARGETS = re.compile(
    r"(CREATE TABLE IF NOT EXISTS|CREATE TABLE|ALTER TABLE)\s+"
    r"apollo_(subjects|concepts|concept_problems|kg_entities|problem_attempts)\b"
)
_MIGRATION_030 = MIGRATIONS_DIR / "030_apollo_autoprovisioning.sql"
_MIGRATION_036 = MIGRATIONS_DIR / "036_apollo_ingest_page_evidence.sql"
_EXCLUDE_FROM_CHAIN = frozenset({_MIGRATION_030.name, "028_apollo_learner_janitor.sql"})


def _chain_migrations() -> list[Path]:
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


async def _apply_chain(dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(_STUB_DDL)
        for migration in _chain_migrations():
            await conn.execute(migration.read_text(encoding="utf-8"))
        await conn.execute(_MIGRATION_030.read_text(encoding="utf-8"))
        await conn.execute(_MIGRATION_036.read_text(encoding="utf-8"))
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def _migrated_dsn(_pg_url: str):
    base_db = make_url(_pg_url).database
    assert base_db, "container URL has no database name"
    admin_dsn = _plain_dsn(_pg_url, base_db)
    migrated_dsn = _plain_dsn(_pg_url, MIGRATED_DB_NAME)

    async def _setup() -> None:
        await _create_db(admin_dsn, MIGRATED_DB_NAME)
        await _apply_chain(migrated_dsn)

    asyncio.run(_setup())
    yield migrated_dsn


@pytest_asyncio.fixture
async def mig_conn(_migrated_dsn: str):
    conn = await asyncpg.connect(_migrated_dsn)
    tr = conn.transaction()
    await tr.start()
    try:
        yield conn
    finally:
        await tr.rollback()
        await conn.close()


async def test_036_adds_n_pages_column(mig_conn):
    row = await mig_conn.fetchrow(
        "SELECT data_type, is_nullable, column_default "
        "FROM information_schema.columns "
        "WHERE table_name='apollo_ingest_runs' AND column_name='n_pages'"
    )
    assert row is not None, "036 did not add apollo_ingest_runs.n_pages"
    assert row["data_type"] == "integer"
    assert row["is_nullable"] == "NO"
    assert "0" in (row["column_default"] or "")


async def test_036_drops_run_document_id_not_null(mig_conn):
    """The run is opened BEFORE indexing (document_id unknown), so 036 relaxes
    apollo_ingest_runs.document_id to NULLABLE — a failed indexing run has no doc."""
    is_nullable = await mig_conn.fetchval(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name='apollo_ingest_runs' AND column_name='document_id'"
    )
    assert is_nullable == "YES"
    # A run row with NO document id is now insertable (the failure-path shape).
    space_id = await mig_conn.fetchval("INSERT INTO aita_search_spaces DEFAULT VALUES RETURNING id")
    run_id = await mig_conn.fetchval(
        "INSERT INTO apollo_ingest_runs (search_space_id, status) "
        "VALUES ($1, 'failed') RETURNING id",
        space_id,
    )
    assert run_id is not None


async def test_036_creates_page_evidence_table(mig_conn):
    cols = {
        r["column_name"]: r
        for r in await mig_conn.fetch(
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_name='apollo_ingest_page_evidence'"
        )
    }
    assert cols, "036 did not create apollo_ingest_page_evidence"
    for expected in (
        "ingest_run_id",
        "search_space_id",
        "document_id",
        "role",
        "page_number",
        "ocr_text",
        "ocr_confidence",
        "extraction_mode",
        "verify_path_fired",
    ):
        assert expected in cols, f"missing column {expected}"
    assert cols["role"]["is_nullable"] == "NO"
    assert cols["ingest_run_id"]["is_nullable"] == "NO"
    # document_id is NULLABLE: a page captured before the failure path minted a
    # document (a chunkless PDF) still gets its OCR evidence persisted.
    assert cols["document_id"]["is_nullable"] == "YES"
    assert cols["verify_path_fired"]["data_type"] == "boolean"
    assert cols["ocr_confidence"]["data_type"] == "real"


async def test_036_evidence_rls_enabled(mig_conn):
    enabled = await mig_conn.fetchval(
        "SELECT relrowsecurity FROM pg_class WHERE relname='apollo_ingest_page_evidence'"
    )
    assert enabled is True


async def test_036_evidence_cascades_from_run(mig_conn):
    """Deleting the ingest run CASCADE-deletes its page evidence."""
    space_id = await mig_conn.fetchval("INSERT INTO aita_search_spaces DEFAULT VALUES RETURNING id")
    run_id = await mig_conn.fetchval(
        "INSERT INTO apollo_ingest_runs (search_space_id, document_id) "
        "VALUES ($1, 42) RETURNING id",
        space_id,
    )
    await mig_conn.execute(
        "INSERT INTO apollo_ingest_page_evidence "
        "(ingest_run_id, search_space_id, document_id, role, page_number, "
        " ocr_text, ocr_confidence, verify_path_fired) "
        "VALUES ($1, $2, 42, 'problem', 1, 'hi', 0.4, true)",
        run_id,
        space_id,
    )
    assert (
        await mig_conn.fetchval(
            "SELECT count(*) FROM apollo_ingest_page_evidence WHERE ingest_run_id=$1",
            run_id,
        )
        == 1
    )
    await mig_conn.execute("DELETE FROM apollo_ingest_runs WHERE id=$1", run_id)
    assert (
        await mig_conn.fetchval(
            "SELECT count(*) FROM apollo_ingest_page_evidence WHERE ingest_run_id=$1",
            run_id,
        )
        == 0
    )


async def test_036_is_idempotent(_migrated_dsn):
    """Re-apply 036 on the already-migrated DB: guarded DDL → no error, and the
    n_pages column + evidence table still exist exactly once."""
    conn = await asyncpg.connect(_migrated_dsn)
    try:
        await conn.execute(_MIGRATION_036.read_text(encoding="utf-8"))
        n_cols = await conn.fetchval(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name='apollo_ingest_runs' AND column_name='n_pages'"
        )
        n_tables = await conn.fetchval(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_name='apollo_ingest_page_evidence'"
        )
        assert n_cols == 1
        assert n_tables == 1
    finally:
        await conn.close()
