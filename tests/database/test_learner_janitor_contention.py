"""WU-5B3a-1 — H2: the SKIP-LOCKED two-claimer concurrency proof (load-bearing).

The savepoint ``db_session`` fixture is a SINGLE NullPool connection inside an
uncommitted outer transaction — it CANNOT make a claimed-and-committed row visible
to a genuinely independent connection, nor demonstrate a row lock SKIPPED by a
second committed transaction. SKIP-LOCKED is only observable with TWO real
committed connections, so H2 builds a fully-migrated DB (the chain incl. 028) on
the session pgvector container (raw asyncpg, mirroring
``test_apollo_learner_model_migration.py``) and runs ``drain_pending_attempts``
against a REAL pooled async engine bound to that committed DSN.

``drain_pending_attempts`` opens its OWN ``get_async_session()`` per phase, so we
patch the janitor's ``get_async_session`` to a factory backed by a real pooled
engine on the migrated DSN — each phase checks out a genuinely independent,
committed connection (NOT a savepoint). The WORK callees
(``run_graph_simulation`` / ``run_learner_update``) are patched to no-ops so the
assertion isolates the CLAIM lock + lease on COMMITTED rows.

MUST RUN GREEN (not skip) with Docker up — a skip is a FAIL of the real-infra gate.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from apollo.handlers import learner_janitor

# Register the Apollo ORM on Base.metadata before any create_all elsewhere.
from apollo.persistence import models as _apollo_models  # noqa: F401

pytestmark = pytest.mark.integration

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "database" / "migrations"
MIGRATED_DB_NAME = "apollo_janitor_contention_migrations"

_STUB_DDL = """
CREATE SCHEMA IF NOT EXISTS auth;
CREATE TABLE auth.users (id UUID PRIMARY KEY);
CREATE TABLE aita_search_spaces (id SERIAL PRIMARY KEY);
"""

_TOUCHES_TARGETS = re.compile(
    r"(CREATE TABLE IF NOT EXISTS|CREATE TABLE|ALTER TABLE)\s+"
    r"apollo_(subjects|concepts|problem_attempts)\b"
)


def _chain_migrations() -> list[Path]:
    return [
        path
        for path in sorted(MIGRATIONS_DIR.glob("*.sql"))
        if _TOUCHES_TARGETS.search(path.read_text(encoding="utf-8"))
    ]


def _plain_dsn(sqlalchemy_url: str, database: str) -> str:
    url = make_url(sqlalchemy_url).set(drivername="postgresql", database=database)
    return url.render_as_string(hide_password=False)


def _asyncpg_sqlalchemy_url(sqlalchemy_url: str, database: str) -> str:
    url = make_url(sqlalchemy_url).set(drivername="postgresql+asyncpg", database=database)
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
def _contention_dsns(_pg_url: str):
    """(plain asyncpg DSN, SQLAlchemy+asyncpg URL) for a fully-migrated DB."""
    base_db = make_url(_pg_url).database
    assert base_db, "container URL has no database name"
    admin_dsn = _plain_dsn(_pg_url, base_db)
    plain_dsn = _plain_dsn(_pg_url, MIGRATED_DB_NAME)
    sa_url = _asyncpg_sqlalchemy_url(_pg_url, MIGRATED_DB_NAME)

    async def _setup() -> None:
        await _create_db(admin_dsn, MIGRATED_DB_NAME)
        await _apply_chain(plain_dsn)

    asyncio.run(_setup())
    yield plain_dsn, sa_url


@pytest_asyncio.fixture
async def committed_engine(_contention_dsns):
    """A real pooled async engine on the migrated DSN + a function-scoped factory
    that the janitor's get_async_session is patched to use. Truncates the attempt
    rows after each test so the committed DB stays clean (no savepoint rollback)."""
    plain_dsn, sa_url = _contention_dsns
    engine = create_async_engine(sa_url, pool_size=5, max_overflow=5)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @contextlib.asynccontextmanager
    async def _session_factory():
        async with factory() as session:
            yield session

    try:
        yield plain_dsn, _session_factory
    finally:
        # Clean every row this test committed (independent connection).
        clean = await asyncpg.connect(plain_dsn)
        try:
            await clean.execute("DELETE FROM apollo_problem_attempts")
            await clean.execute("DELETE FROM apollo_sessions")
            await clean.execute("DELETE FROM apollo_subjects")
            await clean.execute("DELETE FROM aita_search_spaces")
            await clean.execute("DELETE FROM auth.users")
        finally:
            await clean.close()
        await engine.dispose()


async def _seed_committed_due_attempt(plain_dsn: str) -> int:
    """Seed + COMMIT one due pending attempt on an independent connection. Returns
    the attempt id.

    The migration CHAIN (009/018/025/026/028) carries the PRE-auth-retrofit
    ``apollo_sessions`` shape (``student_id`` / ``concept_cluster_id``) — the
    auth-scoping 023 migration does NOT touch the three FK-target tables, so it is
    not in this content-scoped chain. The claim only needs an attempt row that FKs
    to a session; the H2 tests drive the GLOBAL drain (no ``user_id`` join), so the
    legacy session columns are sufficient. (The ``user_id`` scope join is covered
    on the ORM-schema ``db_session`` in H1.)"""
    conn = await asyncpg.connect(plain_dsn)
    try:
        sess = await conn.fetchval(
            "INSERT INTO apollo_sessions (student_id, concept_cluster_id, status, "
            "phase, current_problem_id) "
            "VALUES ($1, 'fluid_mechanics', 'active', 'REPORT', 'p1') RETURNING id",
            str(uuid.uuid4()),
        )
        attempt = await conn.fetchval(
            "INSERT INTO apollo_problem_attempts "
            "(session_id, problem_id, difficulty, result, learner_update_pending) "
            "VALUES ($1, 'p1', 'intro', 'graded', true) RETURNING id",
            sess,
        )
        return attempt
    finally:
        await conn.close()


def _patch_janitor_session(monkeypatch, factory):
    monkeypatch.setattr(learner_janitor, "get_async_session", factory)


# ---------------------------------------------------------------------------
# #19 — two concurrent drainers: EXACTLY ONE claims (SKIP-LOCKED)
# ---------------------------------------------------------------------------


async def test_two_concurrent_drainers_one_claims_via_skip_locked(
    committed_engine, monkeypatch
):
    plain_dsn, factory = committed_engine
    attempt_id = await _seed_committed_due_attempt(plain_dsn)
    _patch_janitor_session(monkeypatch, factory)

    worked: list[int] = []

    async def _work_success(neo, claim):
        worked.append(claim.attempt_id)
        return "success", None

    monkeypatch.setattr(learner_janitor, "_work_one", _work_success)

    r1, r2 = await asyncio.gather(
        learner_janitor.drain_pending_attempts(object(), limit=1),
        learner_janitor.drain_pending_attempts(object(), limit=1),
    )

    # EXACTLY ONE drainer claimed the single committed due row.
    assert {r1.claimed, r2.claimed} == {1, 0}
    assert worked.count(attempt_id) == 1  # worked exactly once
    # the worked row is no longer pending (cleared on success exactly once).
    conn = await asyncpg.connect(plain_dsn)
    try:
        pending = await conn.fetchval(
            "SELECT learner_update_pending FROM apollo_problem_attempts WHERE id=$1",
            attempt_id,
        )
    finally:
        await conn.close()
    assert pending is False


# ---------------------------------------------------------------------------
# #20 — the work-lease prevents a re-claim while drainer 1 is mid-work
# ---------------------------------------------------------------------------


async def test_claim_lease_prevents_reclaim_during_work(committed_engine, monkeypatch):
    plain_dsn, factory = committed_engine
    await _seed_committed_due_attempt(plain_dsn)  # one committed due row
    _patch_janitor_session(monkeypatch, factory)

    released = asyncio.Event()
    paused = asyncio.Event()

    async def _work_paused(neo, claim):
        # Drainer 1 holds the claim (lease already committed) and pauses mid-work.
        paused.set()
        await released.wait()
        return "success", None

    monkeypatch.setattr(learner_janitor, "_work_one", _work_paused)

    async def _drainer1():
        return await learner_janitor.drain_pending_attempts(object(), limit=1)

    task1 = asyncio.create_task(_drainer1())
    await asyncio.wait_for(paused.wait(), timeout=10)

    # Drainer 2 drains while drainer 1 holds the CLAIM_LEASE: the leased row's
    # next_attempt_at is in the future, so it is NOT due -> claimed == 0.
    r2 = await learner_janitor.drain_pending_attempts(object(), limit=1)
    assert r2.claimed == 0

    released.set()
    r1 = await task1
    assert r1.claimed == 1


# ---------------------------------------------------------------------------
# #21 — a committed claim is visible to an independent connection
# ---------------------------------------------------------------------------


async def test_committed_claim_is_visible_to_independent_connection(
    committed_engine, monkeypatch
):
    plain_dsn, factory = committed_engine
    attempt_id = await _seed_committed_due_attempt(plain_dsn)
    _patch_janitor_session(monkeypatch, factory)

    # Patch the work phase to a no-op success so the claim+record commit lands.
    async def _work_success(neo, claim):
        return "success", None

    monkeypatch.setattr(learner_janitor, "_work_one", _work_success)

    before = datetime.now(UTC)
    result = await learner_janitor.drain_pending_attempts(object(), limit=1)
    assert result.claimed == 1

    # A FRESH independent asyncpg connection B sees the committed claim effects.
    conn_b = await asyncpg.connect(plain_dsn)
    try:
        row = await conn_b.fetchrow(
            "SELECT learner_update_attempts, learner_update_pending "
            "FROM apollo_problem_attempts WHERE id=$1",
            attempt_id,
        )
    finally:
        await conn_b.close()
    # attempts bumped at claim (visible across connections), pending cleared on success.
    assert row["learner_update_attempts"] == 1
    assert row["learner_update_pending"] is False
    assert before is not None  # sanity: the drain ran after the seed commit
