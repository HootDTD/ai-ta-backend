"""Real-Postgres tests for migration 028 (Apollo learner-update retry janitor).

WU-5B3a-0 ships ``028_apollo_learner_janitor.sql``: five dead-letter +
exponential-backoff columns on ``apollo_problem_attempts`` plus a PARTIAL index
``apollo_problem_attempts_pending_idx`` over the pending, not-dead rows. The
janitor's claim/recompute state machine (WU-5B3a-1) consumes these; this unit
ships the DDL + ORM mapping only.

DDL behavior (column types/defaults/nullability, the partial-index predicate,
idempotency) is what SQLite cannot honor — so the chain tests apply the *actual*
migration chain (028 auto-joins via the ``ALTER TABLE apollo_problem_attempts``
content glob) to a fresh DB on the session pgvector container, mirroring
``tests/database/test_apollo_learner_model_migration.py``. The ORM round-trip
runs on the savepoint ``db_session`` (``Base.metadata.create_all`` picks up the
five new columns).

These MUST RUN GREEN (not skip) with Docker up — a skip is a FAIL of this gate.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.engine import make_url

# Import the Apollo ORM models at module top so they REGISTER on Base.metadata
# BEFORE the session-scoped `_pg_url` fixture runs `Base.metadata.create_all`
# (the `db_session` schema needs apollo_sessions / apollo_problem_attempts to
# exist for the ORM round-trip). A deferred (in-function) import would register
# them only AFTER create_all, leaving the tables absent.
from apollo.persistence import models as _apollo_models  # noqa: F401

pytestmark = pytest.mark.integration

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "database" / "migrations"
MIGRATED_DB_NAME = "apollo_learner_janitor_migrations"

# FK targets the chain references that no on-disk migration creates.
_STUB_DDL = """
CREATE SCHEMA IF NOT EXISTS auth;
CREATE TABLE auth.users (id UUID PRIMARY KEY);
CREATE TABLE aita_search_spaces (id SERIAL PRIMARY KEY);
"""

# Content scope: every migration that creates/alters the three apollo tables the
# chain needs. 028 ALTERs apollo_problem_attempts, so it auto-joins. Unlike the
# 026 chain test we include EVERY such migration (incl. 026 + 028) — the goal
# here is a fully-migrated table carrying the 028 columns.
_TOUCHES_TARGETS = re.compile(
    r"(CREATE TABLE IF NOT EXISTS|CREATE TABLE|ALTER TABLE)\s+"
    r"apollo_(subjects|concepts|problem_attempts)\b"
)
_MIGRATION_028 = MIGRATIONS_DIR / "028_apollo_learner_janitor.sql"


def _chain_migrations() -> list[Path]:
    """Every migration defining/altering apollo_subjects/concepts/problem_attempts,
    in filename order (028 included)."""
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


# ---------------------------------------------------------------------------
# H2 — migration 028 column shape + partial index + idempotency (raw asyncpg)
# ---------------------------------------------------------------------------

# (column_name, data_type, is_nullable, column_default-predicate)
_EXPECTED_COLUMNS = {
    "learner_update_attempts": ("integer", "NO", lambda d: d is not None and "0" in d),
    "learner_update_failed_at": ("timestamp with time zone", "YES", lambda d: d is None),
    "learner_update_last_error": ("text", "YES", lambda d: d is None),
    "learner_update_next_attempt_at": (
        "timestamp with time zone",
        "YES",
        lambda d: d is None,
    ),
    "learner_update_failed_permanently": (
        "boolean",
        "NO",
        lambda d: d is not None and "false" in d,
    ),
}


async def test_migration_028_adds_five_columns(mig_conn):
    rows = await mig_conn.fetch(
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns "
        "WHERE table_name='apollo_problem_attempts' "
        "AND column_name = ANY($1::text[])",
        list(_EXPECTED_COLUMNS),
    )
    found = {r["column_name"]: r for r in rows}
    assert set(found) == set(_EXPECTED_COLUMNS), "missing 028 column(s)"
    for name, (dtype, nullable, default_ok) in _EXPECTED_COLUMNS.items():
        r = found[name]
        assert r["data_type"] == dtype, f"{name} data_type {r['data_type']!r} != {dtype!r}"
        assert r["is_nullable"] == nullable, f"{name} is_nullable {r['is_nullable']!r}"
        assert default_ok(r["column_default"]), (
            f"{name} column_default {r['column_default']!r} unexpected"
        )


async def test_migration_028_partial_index_exists_and_is_partial(mig_conn):
    # The index exists on the right table.
    idx = await mig_conn.fetchrow(
        "SELECT tablename, indexdef FROM pg_indexes "
        "WHERE indexname='apollo_problem_attempts_pending_idx'"
    )
    assert idx is not None, "partial index missing"
    assert idx["tablename"] == "apollo_problem_attempts"
    # It is PARTIAL: pg_index.indpred is non-null for a partial index.
    indpred = await mig_conn.fetchval(
        "SELECT i.indpred FROM pg_index i "
        "JOIN pg_class c ON c.oid = i.indexrelid "
        "WHERE c.relname='apollo_problem_attempts_pending_idx'"
    )
    assert indpred is not None, "index is not partial (indpred is null)"
    # The predicate references the two boolean columns; created_at is the indexed col.
    assert "learner_update_pending" in idx["indexdef"]
    assert "learner_update_failed_permanently" in idx["indexdef"]
    assert "created_at" in idx["indexdef"]


async def test_migration_028_is_idempotent(_migrated_dsn):
    """Apply 028 a SECOND time on the already-migrated DB; the IF NOT EXISTS
    guards make it a no-op (no error)."""
    conn = await asyncpg.connect(_migrated_dsn)
    try:
        # Re-running must not raise.
        await conn.execute(_MIGRATION_028.read_text(encoding="utf-8"))
        # And the columns/index are still intact afterwards.
        n_cols = await conn.fetchval(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name='apollo_problem_attempts' "
            "AND column_name = ANY($1::text[])",
            list(_EXPECTED_COLUMNS),
        )
        assert n_cols == len(_EXPECTED_COLUMNS)
        idx = await conn.fetchval(
            "SELECT count(*) FROM pg_indexes WHERE indexname='apollo_problem_attempts_pending_idx'"
        )
        assert idx == 1
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# H1 — ORM round-trip of the five new columns on db_session (real PG)
# ---------------------------------------------------------------------------


async def _seed_attempt(db):
    """Minimal session + attempt via ORM (no values for the new cols)."""
    from apollo.conftest import TEST_USER_ID
    from apollo.persistence.models import (
        ApolloSession,
        ProblemAttempt,
        SessionPhase,
        SessionStatus,
    )
    from apollo.subjects.tests._curriculum_fixtures import seed_search_space

    sid = await seed_search_space(db)
    sess = ApolloSession(
        user_id=TEST_USER_ID,
        search_space_id=sid,
        status=SessionStatus.active.value,
        phase=SessionPhase.SOLVING.value,
        current_problem_id="p1",
    )
    db.add(sess)
    await db.flush()
    attempt = ProblemAttempt(session_id=sess.id, problem_id="p1", difficulty="intro")
    db.add(attempt)
    await db.flush()
    return attempt


async def test_problem_attempt_orm_roundtrips_new_columns(db_session):
    from apollo.persistence.models import ProblemAttempt

    attempt = await _seed_attempt(db_session)
    await db_session.commit()

    # Server defaults applied: counts 0, perm-fail False, the three nullable None.
    # populate_existing forces a fresh DB read of the identity-mapped object
    # WITHIN the async execute (greenlet-safe), so we assert the persisted values,
    # not the in-memory ORM defaults.
    read = (
        await db_session.execute(
            select(ProblemAttempt)
            .where(ProblemAttempt.id == attempt.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert read.learner_update_attempts == 0
    assert read.learner_update_failed_permanently is False
    assert read.learner_update_failed_at is None
    assert read.learner_update_last_error is None
    assert read.learner_update_next_attempt_at is None

    # Now set values (incl. tz-aware datetimes) and assert exact round-trip.
    failed_at = datetime(2026, 6, 19, 8, 30, 0, tzinfo=UTC)
    next_at = datetime(2026, 6, 19, 9, 0, 0, tzinfo=UTC)
    read.learner_update_attempts = 2
    read.learner_update_failed_permanently = True
    read.learner_update_last_error = "boom"
    read.learner_update_failed_at = failed_at
    read.learner_update_next_attempt_at = next_at
    await db_session.commit()

    again = (
        await db_session.execute(
            select(ProblemAttempt)
            .where(ProblemAttempt.id == attempt.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert again.learner_update_attempts == 2
    assert again.learner_update_failed_permanently is True
    assert again.learner_update_last_error == "boom"
    # tz preserved (TIMESTAMPTZ): compare the absolute instant.
    assert again.learner_update_failed_at == failed_at
    assert again.learner_update_next_attempt_at == next_at
    assert again.learner_update_failed_at.tzinfo is not None
