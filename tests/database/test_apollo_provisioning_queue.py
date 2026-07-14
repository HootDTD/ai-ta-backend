"""WU-3B2f — the SKIP-LOCKED provisioning queue concurrency proof (load-bearing).

The savepoint ``db_session`` fixture is a SINGLE NullPool connection inside an
uncommitted outer transaction — it CANNOT make a claimed-and-committed row
visible to a genuinely independent connection, nor demonstrate a row lock SKIPPED
by a second committed transaction. SKIP-LOCKED is only observable with TWO real
committed connections, so this file builds a fully-migrated DB (the 030 chain +
030 applied last) on the session pgvector container (raw asyncpg) and runs
``claim_provisioning_job`` against a REAL pooled async engine bound to that
committed DSN, mirroring ``test_learner_janitor_contention.py``.

The migration selection reuses the EXACT content-scoped chain from
``test_apollo_autoprovisioning_migration.py`` (regex + ``_EXCLUDE_FROM_CHAIN`` +
030-applied-last) so ``apollo_provisioning_jobs`` / ``apollo_ingest_runs`` /
``apollo_ingest_errors`` all exist.

Four load-bearing behaviors are pinned here (GREEN-NOT-SKIPPED with Docker up — a
skip is a FAIL of the real-infra gate):
  (1) TWO workers claim NON-OVERLAPPING jobs under contention — no double-claim
      (single job → exactly one claimer wins, the other gets None). NOTE: this
      non-overlap is guaranteed by ``FOR UPDATE`` ALONE (the loser blocks, then
      EvalPlanQual rechecks the now-``running`` row, finds it no longer matches the
      pending/expired predicate, excludes it, and returns the next job or None), so
      dropping ``skip_locked=True`` does NOT RED the two non-overlap tests. What
      SKIP LOCKED additionally buys is NON-BLOCKING LIVENESS — a worker SKIPS an
      already-locked row instead of head-of-line blocking on it — and THAT is the
      property mutation-proved by ``test_skip_locked_skips_locked_row_without_blocking``
      (an externally-held row lock makes a no-skip claim block until timeout → REDs);
  (2) lease-expiry re-claim (a stuck ``running`` row with ``lease_expires_at <
      now()`` is re-claimable, ``attempt_count`` bumps again);
  (3) MAX-attempt dead-letter (``fail_job`` past ``MAX_ATTEMPTS`` → ``failed``);
  (4) cost-abort (``MeteredChat`` ``CostBudgetExceeded`` → ``apollo_ingest_errors``
      row + ``apollo_ingest_runs.status='failed'`` + the job dead-lettered).
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# Register the Apollo ORM on Base.metadata before any create_all elsewhere.
from apollo.persistence import models as _apollo_models  # noqa: F401
from apollo.provisioning.cost_constants import MAX_ATTEMPTS
from apollo.provisioning.metered_chat import CostBudgetExceeded, MeteredChat
from apollo.provisioning.queue import (
    ClaimedJob,
    claim_provisioning_job,
    complete_job,
    fail_job,
    release_job,
)

pytestmark = pytest.mark.integration

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "database" / "migrations"
MIGRATED_DB_NAME = "apollo_provisioning_queue_migrations"

# FK targets no on-disk migration creates (Supabase-managed / app-metadata).
_STUB_DDL = """
CREATE SCHEMA IF NOT EXISTS auth;
CREATE TABLE auth.users (id UUID PRIMARY KEY);
CREATE TABLE aita_search_spaces (id SERIAL PRIMARY KEY);
"""

# Content scope: every table 030 hangs FKs off / ALTERs / whose backfill-join it
# reads, plus the chain prerequisites — copied VERBATIM from the 030 migration
# harness so the resolved chain is [009, 018, 025, 026] and 030 applies last.
_TOUCHES_TARGETS = re.compile(
    r"(CREATE TABLE IF NOT EXISTS|CREATE TABLE|ALTER TABLE)\s+"
    r"apollo_(subjects|concepts|concept_problems|kg_entities|problem_attempts)\b"
)
_MIGRATION_030 = MIGRATIONS_DIR / "030_apollo_autoprovisioning.sql"
_MIGRATION_046 = MIGRATIONS_DIR / "046_apollo_solution_source_llm_paired.sql"
_EXCLUDE_FROM_CHAIN = frozenset(
    {_MIGRATION_030.name, _MIGRATION_046.name, "028_apollo_learner_janitor.sql"}
)


def _chain_migrations() -> list[Path]:
    return [
        path
        for path in sorted(MIGRATIONS_DIR.glob("*.sql"))
        if path.name not in _EXCLUDE_FROM_CHAIN
        and _TOUCHES_TARGETS.search(path.read_text(encoding="utf-8"))
    ]


# Migrations AFTER 030 that extend the apollo_ingest_* observability tables 030
# created (e.g. 036's n_pages column + apollo_ingest_page_evidence). They must run
# AFTER 030 (their ALTER/CREATE reference 030's tables), so they are applied
# separately once 030 is in place rather than folded into the pre-030 chain.
_INGEST_EXTENSION = re.compile(r"apollo_ingest_(runs|errors|page_evidence)")


def _post_030_migrations() -> list[Path]:
    return [
        path
        for path in sorted(MIGRATIONS_DIR.glob("*.sql"))
        if path.name > _MIGRATION_030.name
        and (
            path == _MIGRATION_046
            or (
                path.name not in _EXCLUDE_FROM_CHAIN
                and _INGEST_EXTENSION.search(path.read_text(encoding="utf-8"))
            )
        )
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
        await conn.execute(_MIGRATION_030.read_text(encoding="utf-8"))
        for migration in _post_030_migrations():
            await conn.execute(migration.read_text(encoding="utf-8"))
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def _queue_dsns(_pg_url: str):
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
async def committed_engine(_queue_dsns):
    """A real pooled async engine on the migrated DSN + a function-scoped factory.
    Truncates the four provisioning tables after each test so the committed DB
    stays clean (no savepoint rollback)."""
    plain_dsn, sa_url = _queue_dsns
    engine = create_async_engine(sa_url, pool_size=5, max_overflow=5)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    @contextlib.asynccontextmanager
    async def _session_factory():
        async with factory() as session:
            yield session

    try:
        yield plain_dsn, _session_factory
    finally:
        clean = await asyncpg.connect(plain_dsn)
        try:
            await clean.execute("DELETE FROM apollo_ingest_errors")
            await clean.execute("DELETE FROM apollo_provisioning_jobs")
            await clean.execute("DELETE FROM apollo_ingest_runs")
            await clean.execute("DELETE FROM aita_search_spaces")
        finally:
            await clean.close()
        await engine.dispose()


# --------------------------------------------------------------------------- #
# Seed helpers — independent asyncpg, COMMITTED rows.
# --------------------------------------------------------------------------- #
async def _seed_space(plain_dsn: str) -> int:
    conn = await asyncpg.connect(plain_dsn)
    try:
        return await conn.fetchval("INSERT INTO aita_search_spaces DEFAULT VALUES RETURNING id")
    finally:
        await conn.close()


async def _seed_run(plain_dsn: str, space_id: int, *, document_id: int = 1) -> int:
    conn = await asyncpg.connect(plain_dsn)
    try:
        return await conn.fetchval(
            "INSERT INTO apollo_ingest_runs (search_space_id, document_id) "
            "VALUES ($1, $2) RETURNING id",
            space_id,
            document_id,
        )
    finally:
        await conn.close()


async def _seed_committed_job(
    plain_dsn: str,
    *,
    search_space_id: int,
    document_id: int,
    state: str = "pending",
    attempt_count: int = 0,
    lease_owner: str | None = None,
    lease_expires_at: datetime | None = None,
    ingest_run_id: int | None = None,
    created_at: datetime | None = None,
) -> int:
    """Insert + COMMIT one apollo_provisioning_jobs row on an independent
    connection. Returns the job id."""
    conn = await asyncpg.connect(plain_dsn)
    try:
        cols = ["search_space_id", "document_id", "state", "attempt_count"]
        vals: list = [search_space_id, document_id, state, attempt_count]
        if lease_owner is not None:
            cols.append("lease_owner")
            vals.append(lease_owner)
        if lease_expires_at is not None:
            cols.append("lease_expires_at")
            vals.append(lease_expires_at)
        if ingest_run_id is not None:
            cols.append("ingest_run_id")
            vals.append(ingest_run_id)
        if created_at is not None:
            cols.append("created_at")
            vals.append(created_at)
        placeholders = ", ".join(f"${i + 1}" for i in range(len(vals)))
        sql = (
            f"INSERT INTO apollo_provisioning_jobs ({', '.join(cols)}) "
            f"VALUES ({placeholders}) RETURNING id"
        )
        return await conn.fetchval(sql, *vals)
    finally:
        await conn.close()


async def _read_job(plain_dsn: str, job_id: int) -> asyncpg.Record:
    conn = await asyncpg.connect(plain_dsn)
    try:
        return await conn.fetchrow(
            "SELECT state, lease_owner, lease_expires_at, attempt_count, last_error "
            "FROM apollo_provisioning_jobs WHERE id=$1",
            job_id,
        )
    finally:
        await conn.close()


async def _claim_once(factory, *, lease_owner: str, lease_seconds: int = 300):
    async with factory() as session:
        return await claim_provisioning_job(
            session, lease_owner=lease_owner, lease_seconds=lease_seconds
        )


# ===========================================================================
# Behavior 1 — non-overlapping claims under contention + the SKIP-LOCKED proof.
# ===========================================================================
async def test_two_workers_claim_non_overlapping_jobs(committed_engine):
    plain_dsn, factory = committed_engine
    space = await _seed_space(plain_dsn)
    j1 = await _seed_committed_job(plain_dsn, search_space_id=space, document_id=1)
    j2 = await _seed_committed_job(plain_dsn, search_space_id=space, document_id=2)

    c1, c2 = await asyncio.gather(
        _claim_once(factory, lease_owner="w1"),
        _claim_once(factory, lease_owner="w2"),
    )

    assert c1 is not None and c2 is not None
    assert {c1.job_id, c2.job_id} == {j1, j2}  # distinct, covering both rows


async def test_concurrent_single_job_no_double_claim(committed_engine):
    # A SINGLE seeded job under two concurrent claims: exactly one wins, the other
    # gets None (no double-claim). This pins the non-overlap INVARIANT and holds
    # under FOR UPDATE with OR without SKIP LOCKED — it does NOT by itself prove
    # skip_locked is wired (FOR UPDATE alone serializes the two claimers via
    # lock-wait + EvalPlanQual requery, so the loser still gets None). The genuine
    # skip_locked discriminator is test_skip_locked_skips_locked_row_without_blocking.
    plain_dsn, factory = committed_engine
    space = await _seed_space(plain_dsn)
    job = await _seed_committed_job(plain_dsn, search_space_id=space, document_id=1)

    c1, c2 = await asyncio.gather(
        _claim_once(factory, lease_owner="w1"),
        _claim_once(factory, lease_owner="w2"),
    )

    winners = [c for c in (c1, c2) if c is not None]
    assert len(winners) == 1
    assert winners[0].job_id == job
    assert {c1 is None, c2 is None} == {True, False}


async def test_skip_locked_skips_locked_row_without_blocking(committed_engine):
    # GENUINE skip_locked discriminator. An EXTERNAL connection holds an exclusive
    # row lock on the FIFO-first job and never releases it for the duration of the
    # claim. With skip_locked=True the claim SKIPS the locked row and returns the
    # SECOND job promptly. DROPPING skip_locked=True makes the claim BLOCK on the
    # held lock (FOR UPDATE waits) until asyncio.wait_for times out → TimeoutError
    # → this REDs. This is the ONLY test in the file that fails when skip_locked is
    # removed; the non-overlap tests above intentionally do not (FOR UPDATE alone
    # gives non-overlap), so this pins SKIP LOCKED's non-blocking-liveness contract.
    plain_dsn, factory = committed_engine
    space = await _seed_space(plain_dsn)
    older_ts = datetime.now(UTC) - timedelta(seconds=120)
    newer_ts = datetime.now(UTC) - timedelta(seconds=10)
    locked = await _seed_committed_job(
        plain_dsn, search_space_id=space, document_id=1, created_at=older_ts
    )
    free = await _seed_committed_job(
        plain_dsn, search_space_id=space, document_id=2, created_at=newer_ts
    )

    lock_conn = await asyncpg.connect(plain_dsn)
    tx = lock_conn.transaction()
    await tx.start()
    # Hold an exclusive row lock on the FIFO-first job for the whole claim.
    await lock_conn.execute(
        "SELECT id FROM apollo_provisioning_jobs WHERE id=$1 FOR UPDATE", locked
    )
    try:
        claimed = await asyncio.wait_for(_claim_once(factory, lease_owner="w1"), timeout=5)
    finally:
        await tx.rollback()
        await lock_conn.close()

    # skip_locked skipped the externally-locked FIFO-first row and took the free one.
    assert claimed is not None
    assert claimed.job_id == free


# ===========================================================================
# Claim mechanics.
# ===========================================================================
async def test_claim_sets_lease_and_bumps_attempt(committed_engine):
    plain_dsn, factory = committed_engine
    space = await _seed_space(plain_dsn)
    run = await _seed_run(plain_dsn, space)
    job = await _seed_committed_job(
        plain_dsn, search_space_id=space, document_id=1, ingest_run_id=run
    )

    before = datetime.now(UTC)
    claimed = await _claim_once(factory, lease_owner="w1", lease_seconds=300)
    assert isinstance(claimed, ClaimedJob)
    assert claimed.job_id == job
    assert claimed.search_space_id == space
    assert claimed.document_id == 1
    assert claimed.ingest_run_id == run
    assert claimed.attempt_count == 1

    row = await _read_job(plain_dsn, job)
    assert row["state"] == "running"
    assert row["lease_owner"] == "w1"
    assert row["attempt_count"] == 1
    # lease ~ now + 300s (committed, visible to an independent connection).
    lease = row["lease_expires_at"]
    assert lease is not None
    assert lease > before + timedelta(seconds=250)
    assert lease < before + timedelta(seconds=350)


async def test_claim_returns_none_when_empty(committed_engine):
    _plain_dsn_, factory = committed_engine
    claimed = await _claim_once(factory, lease_owner="w1")
    assert claimed is None


async def test_running_job_with_live_lease_not_claimed(committed_engine):
    plain_dsn, factory = committed_engine
    space = await _seed_space(plain_dsn)
    future = datetime.now(UTC) + timedelta(seconds=600)
    await _seed_committed_job(
        plain_dsn,
        search_space_id=space,
        document_id=1,
        state="running",
        lease_owner="other",
        lease_expires_at=future,
        attempt_count=1,
    )
    claimed = await _claim_once(factory, lease_owner="w1")
    assert claimed is None


# ===========================================================================
# Behavior 2 — lease-expiry re-claim.
# ===========================================================================
async def test_lease_expiry_makes_running_job_reclaimable(committed_engine):
    plain_dsn, factory = committed_engine
    space = await _seed_space(plain_dsn)
    past = datetime.now(UTC) - timedelta(seconds=60)
    job = await _seed_committed_job(
        plain_dsn,
        search_space_id=space,
        document_id=1,
        state="running",
        lease_owner="dead-worker",
        lease_expires_at=past,
        attempt_count=1,
    )
    claimed = await _claim_once(factory, lease_owner="w2")
    assert claimed is not None
    assert claimed.job_id == job
    assert claimed.attempt_count == 2  # bumped again

    row = await _read_job(plain_dsn, job)
    assert row["lease_owner"] == "w2"
    assert row["attempt_count"] == 2


# ===========================================================================
# Terminal transitions.
# ===========================================================================
async def test_complete_job_terminal_and_unclaimable(committed_engine):
    plain_dsn, factory = committed_engine
    space = await _seed_space(plain_dsn)
    job = await _seed_committed_job(plain_dsn, search_space_id=space, document_id=1)

    claimed = await _claim_once(factory, lease_owner="w1")
    assert claimed is not None
    async with factory() as session:
        await complete_job(session, job_id=job)

    row = await _read_job(plain_dsn, job)
    assert row["state"] == "completed"
    assert row["lease_owner"] is None
    assert row["lease_expires_at"] is None

    # A subsequent claim finds nothing.
    assert await _claim_once(factory, lease_owner="w2") is None


async def test_fail_job_retry_below_max(committed_engine):
    plain_dsn, factory = committed_engine
    space = await _seed_space(plain_dsn)
    job = await _seed_committed_job(
        plain_dsn,
        search_space_id=space,
        document_id=1,
        state="running",
        lease_owner="w1",
        attempt_count=1,
    )
    async with factory() as session:
        result = await fail_job(session, job_id=job, error="transient boom")
    assert result == "pending"

    row = await _read_job(plain_dsn, job)
    assert row["state"] == "pending"
    assert row["lease_owner"] is None
    assert row["last_error"] == "transient boom"

    # re-claimable
    claimed = await _claim_once(factory, lease_owner="w2")
    assert claimed is not None
    assert claimed.job_id == job


async def test_fail_job_dead_letters_at_max(committed_engine):
    # MUTATION: a `>` instead of `>=` lets a job at exactly MAX re-enter → REDs.
    plain_dsn, factory = committed_engine
    space = await _seed_space(plain_dsn)
    job = await _seed_committed_job(
        plain_dsn,
        search_space_id=space,
        document_id=1,
        state="running",
        lease_owner="w1",
        attempt_count=MAX_ATTEMPTS,
    )
    async with factory() as session:
        result = await fail_job(session, job_id=job, error="poison")
    assert result == "failed"

    row = await _read_job(plain_dsn, job)
    assert row["state"] == "failed"
    assert row["lease_owner"] is None
    assert row["last_error"] == "poison"

    # No further claim possible (terminal).
    assert await _claim_once(factory, lease_owner="w2") is None


async def test_release_job_returns_to_pending_no_attempt_change(committed_engine):
    plain_dsn, factory = committed_engine
    space = await _seed_space(plain_dsn)
    job = await _seed_committed_job(plain_dsn, search_space_id=space, document_id=1)

    claimed = await _claim_once(factory, lease_owner="w1")
    assert claimed is not None
    assert claimed.attempt_count == 1
    async with factory() as session:
        await release_job(session, job_id=job)

    row = await _read_job(plain_dsn, job)
    assert row["state"] == "pending"
    assert row["lease_owner"] is None
    assert row["attempt_count"] == 1  # UNCHANGED — cooperative release ≠ failure


# ===========================================================================
# Behavior 4 — cost-abort end-to-end on real PG.
# ===========================================================================
async def test_cost_abort_writes_ingest_error_and_fails_run(committed_engine):
    plain_dsn, factory = committed_engine
    space = await _seed_space(plain_dsn)
    run_id = await _seed_run(plain_dsn, space)
    job = await _seed_committed_job(
        plain_dsn,
        search_space_id=space,
        document_id=1,
        ingest_run_id=run_id,
        attempt_count=MAX_ATTEMPTS,
    )

    # Drive a real ingest_run ORM row through MeteredChat with a fake client whose
    # single response crosses a tiny ceiling → CostBudgetExceeded.
    class _Usage:
        prompt_tokens = 60
        completion_tokens = 60

    class _Msg:
        content = "x"

    class _Choice:
        message = _Msg()

    class _Resp:
        usage = _Usage()
        choices = [_Choice()]

    class _Completions:
        def create(self, **kwargs):
            return _Resp()

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    from apollo.persistence.models import IngestRun

    async with factory() as session:
        ingest_run = await session.get(IngestRun, run_id)
        metered = MeteredChat(ingest_run=ingest_run, client=_Client(), ceiling=100, document_id=1)
        raised = False
        try:
            metered.cheap(
                purpose="scrape",
                messages=[{"role": "user", "content": "c"}],
                model="gpt-4o-mini",
            )
        except CostBudgetExceeded:
            raised = True
            # Orchestrator-style terminal handling (production write site is 3B2g):
            ingest_run.status = "failed"
            session.add(
                _apollo_models.IngestError(
                    ingest_run_id=run_id,
                    search_space_id=space,
                    stage="scrape",
                    error_class="CostBudgetExceeded",
                )
            )
            await session.commit()
        assert raised
        assert ingest_run.llm_calls == 1
        assert ingest_run.llm_cost_usd > Decimal("0")

    # The job is dead-lettered at MAX.
    async with factory() as session:
        result = await fail_job(session, job_id=job, error="CostBudgetExceeded")
    assert result == "failed"

    # Assert: error row exists, run failed, job failed — all COMMITTED.
    conn = await asyncpg.connect(plain_dsn)
    try:
        n_err = await conn.fetchval(
            "SELECT count(*) FROM apollo_ingest_errors WHERE ingest_run_id=$1 "
            "AND error_class='CostBudgetExceeded'",
            run_id,
        )
        run_status = await conn.fetchval(
            "SELECT status FROM apollo_ingest_runs WHERE id=$1", run_id
        )
        job_state = await conn.fetchval(
            "SELECT state FROM apollo_provisioning_jobs WHERE id=$1", job
        )
    finally:
        await conn.close()
    assert n_err == 1
    assert run_status == "failed"
    assert job_state == "failed"


# ===========================================================================
# FIFO fairness.
# ===========================================================================
async def test_claim_order_is_fifo(committed_engine):
    plain_dsn, factory = committed_engine
    space = await _seed_space(plain_dsn)
    older_ts = datetime.now(UTC) - timedelta(seconds=120)
    newer_ts = datetime.now(UTC) - timedelta(seconds=10)
    older = await _seed_committed_job(
        plain_dsn, search_space_id=space, document_id=1, created_at=older_ts
    )
    await _seed_committed_job(plain_dsn, search_space_id=space, document_id=2, created_at=newer_ts)

    claimed = await _claim_once(factory, lease_owner="w1")
    assert claimed is not None
    assert claimed.job_id == older  # earliest created_at claimed first
