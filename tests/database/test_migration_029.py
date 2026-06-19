"""Real-Postgres tests for migration 029 (chat-turn keywords JSONB column).

WU-5B4 ships ``029_chat_turn_keywords.sql``: ONE write-only column
``chat_turns.keywords JSONB NOT NULL DEFAULT '[]'::jsonb`` (the §10 RQ5 hedge —
persist the per-/ask ``extract_and_filter_keywords`` output for offline
class-level backfill). The column's NOT-NULL + server-default + idempotency
semantics are exactly what SQLite cannot honor, so these tests apply the ACTUAL
in-order chat-table migration chain (005 → 011 → 015 → 029) to a fresh DB on the
session pgvector container via raw asyncpg, mirroring
``tests/database/test_apollo_learner_janitor_migration.py``.

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
MIGRATED_DB_NAME = "chat_turn_keywords_migrations"

# FK / type targets the chat-table chain references that no chat migration
# creates: auth.users (chat_sessions.user_id), aita_search_spaces
# (chat_sessions.search_space_id), aita_chunks (015 chat_session_snippets.chunk_id),
# the pgvector extension (015 chat_sessions.topic_centroid_vector vector(3072)),
# and the Supabase auth.uid() shim that the 015 RLS policies reference at
# CREATE POLICY time (absent on the bare pgvector image).
_STUB_DDL = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS auth;
CREATE TABLE IF NOT EXISTS auth.users (id UUID PRIMARY KEY);
CREATE OR REPLACE FUNCTION auth.uid() RETURNS uuid AS $$
  SELECT NULLIF(current_setting('request.jwt.claims', true)::json->>'sub', '')::uuid
$$ LANGUAGE sql STABLE;
CREATE TABLE IF NOT EXISTS aita_search_spaces (id SERIAL PRIMARY KEY);
CREATE TABLE IF NOT EXISTS aita_chunks (id BIGSERIAL PRIMARY KEY);
"""

# Content scope: every migration that creates/alters chat_sessions/chat_turns.
# 005 creates both; 011 ALTERs chat_turns (citations); 015 ALTERs chat_sessions
# (topic_centroid) + creates chat_session_snippets/router tables; 029 ALTERs
# chat_turns (keywords) and auto-joins via this glob.
_TOUCHES_TARGETS = re.compile(
    r"(CREATE TABLE IF NOT EXISTS|CREATE TABLE|ALTER TABLE)\s+"
    r"chat_(sessions|turns)\b"
)
_MIGRATION_029 = MIGRATIONS_DIR / "029_chat_turn_keywords.sql"


def _chain_migrations() -> list[Path]:
    return [
        path
        for path in sorted(MIGRATIONS_DIR.glob("*.sql"))
        if _TOUCHES_TARGETS.search(path.read_text(encoding="utf-8"))
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
    """Connection to the fully-migrated DB; everything rolls back per test."""
    conn = await asyncpg.connect(_migrated_dsn)
    tr = conn.transaction()
    await tr.start()
    try:
        yield conn
    finally:
        await tr.rollback()
        await conn.close()


def test_029_chain_includes_expected_migrations():
    """Sanity: the content glob picks up 005, 011, 015, and 029 (in order)."""
    names = [p.name for p in _chain_migrations()]
    assert "005_chat_memory.sql" in names
    assert "011_chat_turn_citations.sql" in names
    assert "015_rag_orchestrator.sql" in names
    assert "029_chat_turn_keywords.sql" in names
    # 029 is last (highest number) in the in-order chain.
    assert names[-1] == "029_chat_turn_keywords.sql"


async def test_migration_029_adds_keywords_column(mig_conn):
    row = await mig_conn.fetchrow(
        "SELECT data_type, is_nullable, column_default "
        "FROM information_schema.columns "
        "WHERE table_name='chat_turns' AND column_name='keywords'"
    )
    assert row is not None, "029 did not add chat_turns.keywords"
    assert row["data_type"] == "jsonb"
    assert row["is_nullable"] == "NO"
    assert row["column_default"] is not None
    assert "'[]'::jsonb" in row["column_default"]


async def test_migration_029_is_idempotent(_migrated_dsn):
    """Re-apply 029 on the already-migrated DB; IF NOT EXISTS → no error, and the
    column still exists exactly once."""
    conn = await asyncpg.connect(_migrated_dsn)
    try:
        await conn.execute(_MIGRATION_029.read_text(encoding="utf-8"))
        n = await conn.fetchval(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name='chat_turns' AND column_name='keywords'"
        )
        assert n == 1
    finally:
        await conn.close()


async def test_existing_rows_default_to_empty_array(mig_conn):
    """A legacy-shaped INSERT that omits keywords reads back [] (server default),
    proving NOT NULL + server_default makes raw/legacy inserts safe."""
    # Seed the FK chain: auth.users → aita_search_spaces → chat_sessions.
    uid = "a0000000-0000-4000-8000-0000000000aa"
    await mig_conn.execute("INSERT INTO auth.users (id) VALUES ($1)", uid)
    ssid = await mig_conn.fetchval(
        "INSERT INTO aita_search_spaces DEFAULT VALUES RETURNING id"
    )
    sess_id = await mig_conn.fetchval(
        "INSERT INTO chat_sessions (chat_id, user_id, search_space_id) "
        "VALUES ('c-legacy', $1, $2) RETURNING id",
        uid,
        ssid,
    )
    # Legacy insert — explicitly omits the keywords column.
    turn_id = await mig_conn.fetchval(
        "INSERT INTO chat_turns (chat_session_id, turn_index, turn_id, role, content) "
        "VALUES ($1, 1, '1', 'assistant', 'x') RETURNING id",
        sess_id,
    )
    raw = await mig_conn.fetchval(
        "SELECT keywords FROM chat_turns WHERE id=$1", turn_id
    )
    # asyncpg returns jsonb as a JSON text string.
    import json

    assert json.loads(raw) == []
