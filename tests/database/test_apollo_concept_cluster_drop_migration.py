"""Real-Postgres behavioral tests for migration 027 (drop concept_cluster_id).

WU-3D §8A cuts the Apollo runtime over to a DB-resolved, course-scoped
curriculum. Once nothing reads or writes ``apollo_sessions.concept_cluster_id``
(Tasks 4/6 prove this), migration ``027_apollo_drop_concept_cluster_id.sql``
drops the legacy column.

The value of this unit is DDL behavior (DROP COLUMN idempotency, that existing
rows survive, that ``concept_id`` is untouched), which SQLite can't honor. So
these tests apply the actual ``apollo_sessions`` migration chain + 027 to a
fresh database on the session pgvector container (Testcontainers), mirroring
``tests/database/test_apollo_learner_model_migration.py``.

Migration selection is content-scoped: the migrations that create/alter
``apollo_sessions`` into the pre-027 shape (009 creates it with
``concept_cluster_id``; 018 adds the ``concept_id`` FK; 023 re-keys to
``user_id``/``search_space_id``), then 027. The RLS-stopgap 022 is excluded
(it adds policies referencing roles not present in a bare container and does not
change ``apollo_sessions``'s column shape). FK-stub targets (``auth.users``,
``aita_search_spaces``, ``course_memberships``, ``apollo_concepts``) are stubbed.
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
MIGRATED_DB_NAME = "apollo_concept_cluster_drop_migrations"
PRE027_DB_PREFIX = "apollo_concept_cluster_drop_pre027"

_MIGRATION_027 = MIGRATIONS_DIR / "027_apollo_drop_concept_cluster_id.sql"

# FK / read targets the apollo_sessions chain references that no on-disk
# migration creates in this stripped chain. Stubbed with the minimal shape the
# DDL resolves against. apollo_concepts is stubbed (018's FK target) so 018's
# concept_id FK resolves without pulling apollo_subjects in.
_STUB_DDL = """
CREATE SCHEMA IF NOT EXISTS auth;
CREATE TABLE auth.users (id UUID PRIMARY KEY);
CREATE TABLE aita_search_spaces (id SERIAL PRIMARY KEY);
CREATE TABLE course_memberships (user_id UUID, search_space_id INTEGER);
CREATE TABLE apollo_concepts (id BIGSERIAL PRIMARY KEY);
"""

# Content scope: only the migrations that shape apollo_sessions' columns into the
# pre-027 state. 009 (create + concept_cluster_id), 013 (apollo_student_progress,
# which 023 re-keys in the same file), 018 (concept_id FK), 023 (user_id /
# search_space_id re-key). Excludes 022 (RLS stopgap) and 026 (does not touch
# apollo_sessions).
_CHAIN_NAMES = (
    "009_apollo_slice0.sql",
    "013_apollo_student_progress.sql",
    "018_apollo_concept_registry.sql",
    "023_apollo_auth_scoping.sql",
)

# 018 ALSO creates apollo_subjects/apollo_concepts/apollo_concept_problems; the
# stub already defines apollo_concepts, so we strip 018 down to its
# apollo_sessions ALTER to avoid a duplicate-table collision with the stub.
_018_SESSIONS_BLOCK = re.compile(
    r"ALTER TABLE apollo_sessions\s+ADD COLUMN IF NOT EXISTS concept_id BIGINT\s+"
    r"REFERENCES apollo_concepts\(id\) ON DELETE RESTRICT;"
)


def _chain_sql() -> list[str]:
    sqls: list[str] = []
    for name in _CHAIN_NAMES:
        text = (MIGRATIONS_DIR / name).read_text(encoding="utf-8")
        if name == "018_apollo_concept_registry.sql":
            # Reduce 018 to just the apollo_sessions concept_id FK add (the only
            # part that shapes apollo_sessions); the rest creates curriculum
            # tables already stubbed.
            m = _018_SESSIONS_BLOCK.search(text)
            assert m, "018 apollo_sessions concept_id block not found"
            text = m.group(0)
        sqls.append(text)
    return sqls


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


async def _apply_chain(dsn: str, *, include_027: bool) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(_STUB_DDL)
        for sql in _chain_sql():
            await conn.execute(sql)
        if include_027:
            await conn.execute(_MIGRATION_027.read_text(encoding="utf-8"))
    finally:
        await conn.close()


async def _column_exists(conn: asyncpg.Connection, column: str) -> bool:
    return (
        await conn.fetchval(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name='apollo_sessions' AND column_name=$1",
            column,
        )
    ) is not None


async def _insert_session(conn: asyncpg.Connection, *, concept_id: int) -> int:
    """Insert a pre-027 apollo_sessions row.

    Pre-027 the table still has the NOT NULL ``concept_cluster_id`` column (from
    009), so a value is supplied here; the point of T7.3 is that after 027 drops
    the column the row survives with ``concept_id`` intact. user_id +
    search_space_id are NOT NULL after 023.
    """
    space = await conn.fetchval("INSERT INTO aita_search_spaces DEFAULT VALUES RETURNING id")
    uid = "a0000000-0000-4000-8000-000000000099"
    await conn.execute("INSERT INTO auth.users (id) VALUES ($1)", uid)
    return await conn.fetchval(
        "INSERT INTO apollo_sessions "
        "(user_id, search_space_id, concept_cluster_id, concept_id, status, phase) "
        "VALUES ($1, $2, 'legacy_cluster', $3, 'active', 'TEACHING') RETURNING id",
        uid,
        space,
        concept_id,
    )


# ---------------------------------------------------------------------------
# Fully-migrated DB (chain + 027), per-test transaction rollback.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _migrated_dsn(_pg_url: str):
    base_db = make_url(_pg_url).database
    assert base_db, "container URL has no database name"
    admin_dsn = _plain_dsn(_pg_url, base_db)
    migrated_dsn = _plain_dsn(_pg_url, MIGRATED_DB_NAME)

    async def _setup() -> None:
        await _create_db(admin_dsn, MIGRATED_DB_NAME)
        await _apply_chain(migrated_dsn, include_027=True)

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


async def test_027_drops_concept_cluster_id(mig_conn):
    """T7.1 — after chain + 027, concept_cluster_id is gone and concept_id stays."""
    assert await _column_exists(mig_conn, "concept_cluster_id") is False
    assert await _column_exists(mig_conn, "concept_id") is True


# ---------------------------------------------------------------------------
# Idempotency + data preservation — build their own pre-027 databases.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pre027_factory(_pg_url: str):
    base_db = make_url(_pg_url).database
    admin_dsn = _plain_dsn(_pg_url, base_db)
    created: list[str] = []

    async def _build(seed_coro=None):
        name = f"{PRE027_DB_PREFIX}_{len(created)}"
        created.append(name)
        dsn = _plain_dsn(_pg_url, name)
        await _create_db(admin_dsn, name)
        await _apply_chain(dsn, include_027=False)
        if seed_coro is not None:
            conn = await asyncpg.connect(dsn)
            try:
                await seed_coro(conn)
            finally:
                await conn.close()
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(_MIGRATION_027.read_text(encoding="utf-8"))
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


async def test_027_idempotent(pre027_factory):
    """T7.2 — applying 027 a second time succeeds (DROP COLUMN IF EXISTS)."""
    dsn = await pre027_factory()
    conn = await asyncpg.connect(dsn)
    try:
        # Apply 027 again — must not raise.
        await conn.execute(_MIGRATION_027.read_text(encoding="utf-8"))
        assert await _column_exists(conn, "concept_cluster_id") is False
    finally:
        await conn.close()


async def test_027_preserves_existing_sessions(pre027_factory):
    """T7.3 — a session row inserted (with concept_id) before 027 survives 027
    with concept_id intact."""
    state: dict[str, int] = {}

    async def _seed(conn: asyncpg.Connection):
        concept_id = await conn.fetchval("INSERT INTO apollo_concepts DEFAULT VALUES RETURNING id")
        session_id = await _insert_session(conn, concept_id=concept_id)
        state["session_id"] = session_id
        state["concept_id"] = concept_id

    dsn = await pre027_factory(_seed)
    conn = await asyncpg.connect(dsn)
    try:
        row = await conn.fetchrow(
            "SELECT concept_id FROM apollo_sessions WHERE id=$1", state["session_id"]
        )
        assert row is not None
        assert row["concept_id"] == state["concept_id"]
        assert await _column_exists(conn, "concept_cluster_id") is False
    finally:
        await conn.close()


def test_models_apollo_session_has_no_concept_cluster_id():
    """T7.4 — the ORM dropped the column in lockstep with the migration."""
    from apollo.persistence.models import TutoringSession

    assert "concept_cluster_id" not in TutoringSession.__table__.columns
    assert "concept_id" in TutoringSession.__table__.columns
