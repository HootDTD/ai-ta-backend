"""RLS cross-tenant negative test for ``chat_turns.keywords`` (WU-5B4).

The new column inherits the EXISTING table-level RLS policy
``chat_turns_owner_rw`` (migration 006): a turn is visible only to the owner of
its parent ``chat_sessions`` row (``cs.user_id = auth.uid()``). This proves the
new column does NOT leak across tenants.

The bare ``pgvector/pgvector:pg16`` image has no Supabase ``auth.uid()``
function, so the chain stub defines the standard local shim
(``auth.uid()`` reads ``request.jwt.claims->>'sub'``). The test connects, sets a
NON-superuser role (superusers/owners BYPASS RLS), sets the JWT claim to a given
user, and asserts visibility. MUST RUN GREEN (not skip) with Docker up.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.integration

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "database" / "migrations"
RLS_DB_NAME = "chat_turn_keywords_rls"

USER_A = "a0000000-0000-4000-8000-00000000000a"
USER_B = "b0000000-0000-4000-8000-00000000000b"

# Stub: pgvector ext + auth.uid() shim + the FK / policy-target tables 005/006
# reference (auth.users, aita_search_spaces, aita_chunks, teacher_courses,
# teacher_uploads). The RLS-test role is a plain login role that does NOT bypass
# RLS (unlike the container superuser).
_STUB_DDL = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS auth;
CREATE TABLE IF NOT EXISTS auth.users (id UUID PRIMARY KEY);
CREATE OR REPLACE FUNCTION auth.uid() RETURNS uuid AS $$
  SELECT NULLIF(current_setting('request.jwt.claims', true)::json->>'sub', '')::uuid
$$ LANGUAGE sql STABLE;
CREATE TABLE IF NOT EXISTS aita_search_spaces (id SERIAL PRIMARY KEY);
CREATE TABLE IF NOT EXISTS aita_chunks (id BIGSERIAL PRIMARY KEY);
CREATE TABLE IF NOT EXISTS teacher_courses (search_space_id INTEGER);
CREATE TABLE IF NOT EXISTS teacher_uploads (search_space_id INTEGER);
DROP ROLE IF EXISTS rls_tenant;
CREATE ROLE rls_tenant LOGIN;
GRANT USAGE ON SCHEMA auth TO rls_tenant;
"""

# Migrations needed for the column chain + the 006 RLS policy.
_CHAIN = [
    "005_chat_memory.sql",
    "006_membership_auth.sql",
    "011_chat_turn_citations.sql",
    "015_rag_orchestrator.sql",
    "029_chat_turn_keywords.sql",
]

_GRANT_TENANT = """
GRANT SELECT, INSERT, UPDATE, DELETE ON chat_sessions, chat_turns TO rls_tenant;
GRANT EXECUTE ON FUNCTION auth.uid() TO rls_tenant;
"""


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
        for name in _CHAIN:
            await conn.execute((MIGRATIONS_DIR / name).read_text(encoding="utf-8"))
        await conn.execute(_GRANT_TENANT)
        # Seed: user A owns a session + a turn carrying keywords=["x"].
        await conn.execute("INSERT INTO auth.users (id) VALUES ($1), ($2)", USER_A, USER_B)
        ssid = await conn.fetchval("INSERT INTO aita_search_spaces DEFAULT VALUES RETURNING id")
        sess_id = await conn.fetchval(
            "INSERT INTO chat_sessions (chat_id, user_id, search_space_id) "
            "VALUES ('a-chat', $1, $2) RETURNING id",
            USER_A,
            ssid,
        )
        await conn.execute(
            "INSERT INTO chat_turns "
            "(chat_session_id, turn_index, turn_id, role, content, keywords) "
            "VALUES ($1, 1, '1', 'assistant', 'a', '[\"x\"]'::jsonb)",
            sess_id,
        )
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def _rls_dsn(_pg_url: str):
    base_db = make_url(_pg_url).database
    admin_dsn = _plain_dsn(_pg_url, base_db)
    rls_dsn = _plain_dsn(_pg_url, RLS_DB_NAME)

    async def _setup() -> None:
        await _create_db(admin_dsn, RLS_DB_NAME)
        await _apply_chain(rls_dsn)

    asyncio.run(_setup())
    yield rls_dsn


@pytest_asyncio.fixture
async def tenant_conn(_rls_dsn: str):
    """A connection acting as the non-bypass ``rls_tenant`` role."""
    conn = await asyncpg.connect(_rls_dsn)
    await conn.execute("SET ROLE rls_tenant")
    try:
        yield conn
    finally:
        await conn.execute("RESET ROLE")
        await conn.close()


async def _keywords_visible_to(conn, user_id: str) -> list:
    tr = conn.transaction()
    await tr.start()
    try:
        await conn.execute(
            "SELECT set_config('request.jwt.claims', $1, true)",
            f'{{"sub":"{user_id}"}}',
        )
        return await conn.fetch(
            "SELECT keywords FROM chat_turns "
            "WHERE chat_session_id = (SELECT id FROM chat_sessions WHERE chat_id='a-chat')"
        )
    finally:
        await tr.rollback()


async def test_other_user_sees_zero_keyword_rows(tenant_conn):
    # User B (a different tenant) sees ZERO rows of user A's turns.
    rows_b = await _keywords_visible_to(tenant_conn, USER_B)
    assert rows_b == []


async def test_owner_sees_their_keyword_rows(tenant_conn):
    # User A (the owner) sees the row AND its keywords ["x"].
    import json

    rows_a = await _keywords_visible_to(tenant_conn, USER_A)
    assert len(rows_a) == 1
    assert json.loads(rows_a[0]["keywords"]) == ["x"]
