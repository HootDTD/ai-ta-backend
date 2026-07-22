"""Local-Docker rehearsal for DB-17's pre-cutover duplicate-index drop script.

``scripts/db/drop_duplicate_indexes.sql`` is a separately-gated, human-run
script (see that file's banner) targeting the 13 exact duplicate indexes
captured read-only from PROD via MCP
(``.planning/cleanup/inputs/db-preflight.md``). The reconstructed
``legacy_public_snapshot.sql`` never creates the redundant ``ix_*`` family --
per the preflight capture, those were SQLAlchemy ``index=True`` residue
applied directly to prod outside the numbered SQL migration chain, so they
are simply absent from a snapshot rebuilt purely from that chain. This suite
therefore seeds the 13 ``ix_*`` duplicates by hand on top of the snapshot to
reproduce prod's real duplicate-index shape, then rehearses the drop script
against that shape: every canonical twin (the hand-written ``idx_*`` index or
the implicit unique-constraint index) must survive untouched, every ``ix_*``
duplicate must be gone, and a second run of the same script must be a
complete no-op (``IF EXISTS`` guards).

``DROP INDEX CONCURRENTLY`` cannot run inside a transaction block, so unlike
every other migration/script rehearsal in this directory, the script's
statements are executed one at a time on a bare (non-transactional)
connection rather than as a single multi-statement ``conn.execute()`` call --
sending them as one string would let Postgres's simple-query protocol wrap
the whole batch in an implicit transaction, which is exactly what
CONCURRENTLY forbids.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy.engine import make_url

pytestmark = pytest.mark.integration

_REPO = Path(__file__).resolve().parents[2]
_MIGRATIONS = _REPO / "supabase" / "migrations"
_SNAPSHOT = _MIGRATIONS / "20260717032246_legacy_public_snapshot.sql"
_SCRIPT = _REPO / "scripts" / "db" / "drop_duplicate_indexes.sql"
_DB_NAME = "drop_duplicate_indexes"

# Each pair is (duplicate ``ix_*`` name to seed + drop, canonical surviving
# twin already created by the snapshot -- either a plain index or the
# implicit index backing a UNIQUE constraint). Mirrors
# ``.planning/cleanup/inputs/db-preflight.md`` verbatim.
_DUPLICATE_PAIRS: list[tuple[str, str, str]] = [
    ("ix_aita_chunks_document_id", "aita_chunks", "document_id"),
    ("ix_aita_chunks_page_number", "aita_chunks", "page_number"),
    ("ix_aita_documents_material_kind", "aita_documents", "material_kind"),
    ("ix_aita_documents_search_space_id", "aita_documents", "search_space_id"),
    ("ix_aita_documents_content_hash", "aita_documents", "content_hash"),
    ("ix_aita_documents_unique_identifier_hash", "aita_documents", "unique_identifier_hash"),
    ("ix_aita_search_spaces_name", "aita_search_spaces", "name"),
    ("ix_aita_search_spaces_slug", "aita_search_spaces", "slug"),
    ("ix_chat_sessions_chat_id", "chat_sessions", "chat_id"),
    ("ix_course_invite_links_search_space_id", "course_invite_links", "search_space_id"),
    ("ix_course_invite_links_code", "course_invite_links", "code"),
    ("ix_teacher_courses_search_space_id", "teacher_courses", "search_space_id"),
    ("ix_teacher_upload_jobs_upload_id", "teacher_upload_jobs", "upload_id"),
]

# The canonical twin each duplicate must leave untouched: either a plain
# index name or ``constraint:<name>`` for an implicit unique-constraint index.
_SURVIVING_TWINS = {
    "ix_aita_chunks_document_id": "idx_aita_chunks_document",
    "ix_aita_chunks_page_number": "idx_aita_chunks_page",
    "ix_aita_documents_material_kind": "idx_aita_documents_material_kind",
    "ix_aita_documents_search_space_id": "idx_aita_documents_search_space",
    "ix_aita_documents_content_hash": "aita_documents_content_hash_key",
    "ix_aita_documents_unique_identifier_hash": "aita_documents_unique_identifier_hash_key",
    "ix_aita_search_spaces_name": "idx_aita_search_spaces_name",
    "ix_aita_search_spaces_slug": "aita_search_spaces_slug_key",
    "ix_chat_sessions_chat_id": "chat_sessions_chat_id_key",
    "ix_course_invite_links_search_space_id": "idx_invite_links_space",
    "ix_course_invite_links_code": "course_invite_links_code_key",
    "ix_teacher_courses_search_space_id": "teacher_courses_search_space_id_key",
    "ix_teacher_upload_jobs_upload_id": "idx_teacher_upload_jobs_upload_id",
}

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


def _split_statements(sql_text: str) -> list[str]:
    """Strip ``--`` line comments and split on top-level ``;``.

    The script contains no dollar-quoted bodies (deliberately -- see its
    banner: ``DROP INDEX CONCURRENTLY`` cannot run inside a DO block's
    implicit transaction either), so a comment-strip + naive semicolon split
    is safe and exactly mirrors how ``psql -f`` executes each statement as
    its own autocommit round trip.
    """
    without_comments = re.sub(r"--[^\n]*", "", sql_text)
    statements = [stmt.strip() for stmt in without_comments.split(";")]
    return [stmt for stmt in statements if stmt]


def _dsn(url: str, database: str) -> str:
    return (
        make_url(url)
        .set(drivername="postgresql", database=database)
        .render_as_string(hide_password=False)
    )


@pytest.fixture
def duplicate_index_dsn(request: pytest.FixtureRequest):
    """A fresh legacy-snapshot database seeded with the 13 ``ix_*`` duplicates.

    Function-scoped (like ``test_remove_legacy_public_schema.py``'s
    ``teardown_schema_dsn``): the drop script mutates the schema, so each
    test gets its own throwaway database. Sync fixture calling
    ``asyncio.run()`` for the same reason as its sibling fixtures -- it must
    not execute inside an already-running event loop.
    """
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
            await admin.execute(f'DROP DATABASE IF EXISTS "{_DB_NAME}" WITH (FORCE)')
            await admin.execute(f'CREATE DATABASE "{_DB_NAME}"')
        finally:
            await admin.close()

        conn = await asyncpg.connect(test_dsn)
        try:
            await conn.execute(_AUTH_BOOTSTRAP)
            await conn.execute(_SNAPSHOT.read_text(encoding="utf-8"))
            # Seed the 13 ``ix_*`` duplicates the snapshot itself never
            # creates (see module docstring) so the rehearsal exercises real
            # duplicate-index removal, not 13 no-op ``IF EXISTS`` misses.
            for ix_name, table, column in _DUPLICATE_PAIRS:
                await conn.execute(f"CREATE INDEX {ix_name} ON public.{table} ({column})")
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


async def _index_names(conn: asyncpg.Connection) -> set[str]:
    rows = await conn.fetch("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
    return {row["indexname"] for row in rows}


async def _run_script(dsn: str) -> None:
    """Execute the drop script one statement per connection call.

    Each ``DROP INDEX CONCURRENTLY`` must be its own round trip (see module
    docstring) so Postgres never wraps it in an implicit transaction.
    """
    statements = _split_statements(_SCRIPT.read_text(encoding="utf-8"))
    assert any("drop index concurrently" in s.lower() for s in statements), (
        "sanity check: the script must contain CONCURRENTLY drops"
    )
    conn = await asyncpg.connect(dsn)
    try:
        for statement in statements:
            await conn.execute(statement)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_drop_script_removes_exactly_the_13_duplicates(duplicate_index_dsn):
    conn = await asyncpg.connect(duplicate_index_dsn)
    try:
        before = await _index_names(conn)
    finally:
        await conn.close()

    for ix_name, _table, _column in _DUPLICATE_PAIRS:
        assert ix_name in before, f"fixture setup failed to seed {ix_name}"
    for twin_name in _SURVIVING_TWINS.values():
        assert twin_name in before, f"expected canonical twin {twin_name} missing before drop"

    await _run_script(duplicate_index_dsn)

    conn = await asyncpg.connect(duplicate_index_dsn)
    try:
        after = await _index_names(conn)
        # Every surviving twin must still be present AND valid -- proves the
        # script dropped the redundant member, never the canonical one.
        invalid_rows = await conn.fetch(
            "SELECT c.relname FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
            "WHERE NOT i.indisvalid"
        )
    finally:
        await conn.close()

    dropped = before - after
    assert dropped == set(_SURVIVING_TWINS.keys()), (
        f"expected exactly the 13 ix_* duplicates to drop, got diff: "
        f"missing={set(_SURVIVING_TWINS.keys()) - dropped} unexpected={dropped - set(_SURVIVING_TWINS.keys())}"
    )
    for twin_name in _SURVIVING_TWINS.values():
        assert twin_name in after, f"surviving twin {twin_name} was dropped -- must not happen"
    assert invalid_rows == [], f"unexpected invalid indexes after drop: {invalid_rows}"


@pytest.mark.asyncio
async def test_drop_script_reapply_is_a_no_op(duplicate_index_dsn):
    await _run_script(duplicate_index_dsn)  # first pass: real drops happen

    conn = await asyncpg.connect(duplicate_index_dsn)
    try:
        after_first = await _index_names(conn)
    finally:
        await conn.close()

    await _run_script(duplicate_index_dsn)  # second pass: must not raise

    conn = await asyncpg.connect(duplicate_index_dsn)
    try:
        after_second = await _index_names(conn)
    finally:
        await conn.close()

    assert after_first == after_second, "re-applying the script must be a byte-for-byte no-op"
    for ix_name in _SURVIVING_TWINS:
        assert ix_name not in after_second


def test_script_never_wraps_concurrently_drops_in_a_transaction_block():
    sql = _SCRIPT.read_text(encoding="utf-8")
    assert "BEGIN;" not in sql and "\nBEGIN\n" not in sql, (
        "DROP INDEX CONCURRENTLY cannot run inside an explicit transaction block"
    )
    assert "COMMIT;" not in sql
    for ix_name in _SURVIVING_TWINS:
        assert f"DROP INDEX CONCURRENTLY IF EXISTS public.{ix_name};" in sql


def test_script_documents_the_surviving_twin_for_every_drop():
    sql = _SCRIPT.read_text(encoding="utf-8")
    for ix_name, twin_name in _SURVIVING_TWINS.items():
        # Find the drop line and confirm the very next non-blank line names
        # the twin (either as a plain index or an explicit constraint).
        marker = f"public.{ix_name};"
        idx = sql.index(marker)
        following = sql[idx : idx + 400]
        assert "surviving twin" in following, f"missing surviving-twin comment for {ix_name}"
        assert twin_name in following, f"surviving-twin comment for {ix_name} doesn't name {twin_name}"
