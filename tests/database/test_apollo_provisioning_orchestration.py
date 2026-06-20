"""WU-3B2g — the real-PG orchestration proof (committed-engine + migration-chain).

The savepoint ``db_session`` fixture CANNOT prove the migration-030 partial-
unique-index (it is migration-only, not in ``create_all``) nor cross-connection
COMMITTED visibility, so this file builds a fully-migrated DB (the 030 chain + 030
applied last) on the session pgvector container and runs the WU-3B2g seam against
a REAL pooled async engine — mirroring ``test_apollo_provisioning_queue.py`` (the
harness is ADAPTED, not copied: the teardown DELETEs every table the orchestrator
writes).

GREEN-NOT-SKIPPED with Docker up (a skip is a FAIL of the real-infra gate). The
load-bearing assertions:
  - T-DB1: committed enqueue creates one open job + a queued run, cross-conn visible
  - T-DB2: the partial-unique-index COLLAPSES a duplicate open job (migration-only)
  - T-DB3: content-hash short-circuit — an unchanged re-upload enqueues ZERO jobs
           (MUTATION PROOF a: drop the succeeded-run SELECT -> a job appears -> REDs)
  - T-DB4: lease-reaper re-opens an expired 'running' job + marks its run failed
           (MUTATION PROOF b: drop the reaper's run-fail write -> run stays running)
  - T-DB5: terminal-status reconciliation — a per-document error never leaves running
  - T-DB6: flag-OFF upload-facing columns are untouched + an enqueue IntegrityError
           can never break the caller commit (the load-bearing NON-REGRESSION)
  - T-DB7: Procfile has the apollo-provision line
  - T-DB8: an enqueue collision returns None via begin_nested and the caller commits
  - T-DB9: intra-job re-run skips already-scraped chunks, counts re-assigned not
           doubled (MUTATION PROOF c)
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# Register the Apollo ORM on Base.metadata before any create_all elsewhere.
from apollo.persistence import models as _apollo_models  # noqa: F401
from apollo.persistence.models import (
    ConceptProblem,
    IngestError,
    IngestRun,
    ProvisioningJob,
)
from apollo.provisioning.enqueue import enqueue_provisioning_job
from apollo.provisioning.queue import claim_provisioning_job
from apollo.provisioning.scrape import CandidateQuestion, ScrapeResult

pytestmark = pytest.mark.integration

orch = sys.modules.get("apollo.provisioning.orchestrator")
if orch is None:  # pragma: no cover - import side effect
    import apollo.provisioning.orchestrator as orch  # noqa: F401

from apollo.provisioning.orchestrator import run_provisioning  # noqa: E402
from apollo.provisioning.queue import ClaimedJob  # noqa: E402

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "database" / "migrations"
MIGRATED_DB_NAME = "apollo_provisioning_orchestration_migrations"

# FK targets no on-disk migration creates (Supabase-managed / app-metadata) PLUS
# the aita_documents / aita_chunks the orchestrator reads (app metadata).
_STUB_DDL = """
CREATE SCHEMA IF NOT EXISTS auth;
CREATE TABLE auth.users (id UUID PRIMARY KEY);
CREATE TABLE aita_search_spaces (id SERIAL PRIMARY KEY);
CREATE TABLE aita_documents (
    id BIGSERIAL PRIMARY KEY,
    content_hash TEXT,
    search_space_id INTEGER
);
CREATE TABLE aita_chunks (
    id BIGSERIAL PRIMARY KEY,
    document_id BIGINT NOT NULL,
    content TEXT NOT NULL,
    page_number INTEGER
);
"""

_TOUCHES_TARGETS = re.compile(
    r"(CREATE TABLE IF NOT EXISTS|CREATE TABLE|ALTER TABLE)\s+"
    r"apollo_(subjects|concepts|concept_problems|kg_entities|problem_attempts)\b"
)
_MIGRATION_030 = MIGRATIONS_DIR / "030_apollo_autoprovisioning.sql"
_EXCLUDE_FROM_CHAIN = frozenset(
    {_MIGRATION_030.name, "028_apollo_learner_janitor.sql"}
)


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


def _asyncpg_sqlalchemy_url(sqlalchemy_url: str, database: str) -> str:
    url = make_url(sqlalchemy_url).set(
        drivername="postgresql+asyncpg", database=database
    )
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
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def _orch_dsns(_pg_url: str):
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


# Every table the orchestration seam writes, deleted FK-leaf-first after each test.
_CLEANUP_TABLES = (
    "apollo_ingest_errors",
    "apollo_rejected_problems",
    "apollo_dedup_decisions",
    "apollo_provisioning_jobs",
    "apollo_concept_problems",
    "apollo_kg_entities",
    "apollo_concepts",
    "apollo_subjects",
    "apollo_ingest_runs",
    "aita_chunks",
    "aita_documents",
    "aita_search_spaces",
)


@pytest_asyncio.fixture
async def committed_engine(_orch_dsns):
    plain_dsn, sa_url = _orch_dsns
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
            for table in _CLEANUP_TABLES:
                await clean.execute(f"DELETE FROM {table}")
        finally:
            await clean.close()
        await engine.dispose()


# --------------------------------------------------------------------------- #
# Committed seed helpers (independent asyncpg connections).
# --------------------------------------------------------------------------- #
async def _seed_space(plain_dsn: str) -> int:
    conn = await asyncpg.connect(plain_dsn)
    try:
        return await conn.fetchval(
            "INSERT INTO aita_search_spaces DEFAULT VALUES RETURNING id"
        )
    finally:
        await conn.close()


async def _seed_document(plain_dsn: str, *, space_id: int, content_hash: str) -> int:
    conn = await asyncpg.connect(plain_dsn)
    try:
        return await conn.fetchval(
            "INSERT INTO aita_documents (content_hash, search_space_id) "
            "VALUES ($1, $2) RETURNING id",
            content_hash,
            space_id,
        )
    finally:
        await conn.close()


async def _seed_chunk(plain_dsn: str, *, document_id: int, content: str) -> int:
    conn = await asyncpg.connect(plain_dsn)
    try:
        return await conn.fetchval(
            "INSERT INTO aita_chunks (document_id, content, page_number) "
            "VALUES ($1, $2, 1) RETURNING id",
            document_id,
            content,
        )
    finally:
        await conn.close()


async def _seed_run(
    plain_dsn: str, space_id: int, *, document_id: int, content_hash: str | None,
    status: str = "queued",
) -> int:
    conn = await asyncpg.connect(plain_dsn)
    try:
        return await conn.fetchval(
            "INSERT INTO apollo_ingest_runs (search_space_id, document_id, "
            "content_hash, status) VALUES ($1, $2, $3, $4) RETURNING id",
            space_id,
            document_id,
            content_hash,
            status,
        )
    finally:
        await conn.close()


async def _seed_job(
    plain_dsn: str,
    *,
    space_id: int,
    document_id: int,
    state: str = "pending",
    lease_owner: str | None = None,
    lease_expires_at: datetime | None = None,
    ingest_run_id: int | None = None,
    attempt_count: int = 0,
) -> int:
    conn = await asyncpg.connect(plain_dsn)
    try:
        return await conn.fetchval(
            "INSERT INTO apollo_provisioning_jobs (search_space_id, document_id, "
            "state, lease_owner, lease_expires_at, ingest_run_id, attempt_count) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id",
            space_id,
            document_id,
            state,
            lease_owner,
            lease_expires_at,
            ingest_run_id,
            attempt_count,
        )
    finally:
        await conn.close()


async def _count(plain_dsn: str, sql: str, *args) -> int:
    conn = await asyncpg.connect(plain_dsn)
    try:
        return await conn.fetchval(sql, *args)
    finally:
        await conn.close()


# ===========================================================================
# T-DB1 — committed enqueue creates one open job + a queued run.
# ===========================================================================
async def test_enqueue_creates_open_job_real_pg(committed_engine):
    plain_dsn, factory = committed_engine
    space = await _seed_space(plain_dsn)
    doc = await _seed_document(plain_dsn, space_id=space, content_hash="h1")

    async with factory() as session:
        job_id = await enqueue_provisioning_job(
            session, search_space_id=space, document_id=doc, content_hash="h1"
        )
        await session.commit()
    assert job_id is not None

    # Visible to an INDEPENDENT connection (committed, not savepoint).
    n_open = await _count(
        plain_dsn,
        "SELECT count(*) FROM apollo_provisioning_jobs WHERE document_id=$1 "
        "AND state IN ('pending','running')",
        doc,
    )
    n_queued = await _count(
        plain_dsn,
        "SELECT count(*) FROM apollo_ingest_runs WHERE document_id=$1 "
        "AND status='queued'",
        doc,
    )
    assert n_open == 1
    assert n_queued == 1


# ===========================================================================
# T-DB2 — the partial-unique-index collapses a duplicate open job.
# ===========================================================================
async def test_enqueue_partial_unique_index_collapses_duplicate_open_job(
    committed_engine,
):
    plain_dsn, factory = committed_engine
    space = await _seed_space(plain_dsn)
    doc = await _seed_document(plain_dsn, space_id=space, content_hash="h1")
    # An OPEN job already committed for this document.
    await _seed_job(plain_dsn, space_id=space, document_id=doc, state="pending")

    async with factory() as session:
        result = await enqueue_provisioning_job(
            session, search_space_id=space, document_id=doc, content_hash="h1"
        )
        await session.commit()

    assert result is None  # collapsed by the migration-030 partial-unique-index
    n_open = await _count(
        plain_dsn,
        "SELECT count(*) FROM apollo_provisioning_jobs WHERE document_id=$1 "
        "AND state IN ('pending','running')",
        doc,
    )
    assert n_open == 1  # still exactly one open job


# ===========================================================================
# T-DB3 — content-hash short-circuit (MUTATION PROOF a).
# ===========================================================================
async def test_enqueue_short_circuit_unchanged_reupload(committed_engine):
    plain_dsn, factory = committed_engine
    space = await _seed_space(plain_dsn)
    doc = await _seed_document(plain_dsn, space_id=space, content_hash="h1")
    # A SUCCEEDED run for (doc, 'h1') already exists — an unchanged re-upload.
    await _seed_run(
        plain_dsn, space, document_id=doc, content_hash="h1", status="succeeded"
    )

    async with factory() as session:
        result = await enqueue_provisioning_job(
            session, search_space_id=space, document_id=doc, content_hash="h1"
        )
        await session.commit()

    assert result is None
    # ZERO new jobs: removing the succeeded-run SELECT -> a duplicate job -> REDs.
    n_jobs = await _count(
        plain_dsn,
        "SELECT count(*) FROM apollo_provisioning_jobs WHERE document_id=$1",
        doc,
    )
    assert n_jobs == 0


# ===========================================================================
# T-DB4 — lease-reaper re-opens an expired running job + marks its run failed.
# (MUTATION PROOF b.)
# ===========================================================================
async def test_lease_reaper_reopens_expired_running_job_and_fails_run(
    committed_engine,
):
    from apollo.provision_worker import _reap_expired

    plain_dsn, factory = committed_engine
    space = await _seed_space(plain_dsn)
    doc = await _seed_document(plain_dsn, space_id=space, content_hash="h1")
    run_id = await _seed_run(
        plain_dsn, space, document_id=doc, content_hash="h1", status="running"
    )
    past = datetime.now(UTC) - timedelta(seconds=60)
    job_id = await _seed_job(
        plain_dsn,
        space_id=space,
        document_id=doc,
        state="running",
        lease_owner="dead-worker",
        lease_expires_at=past,
        ingest_run_id=run_id,
        attempt_count=1,
    )
    # A SECOND expired 'running' job with NO linked ingest_run — the reaper must
    # SKIP it (no run to fail) and not crash. Distinct document_id so the open-uniq
    # index does not reject it.
    doc2 = await _seed_document(plain_dsn, space_id=space, content_hash="h2")
    await _seed_job(
        plain_dsn,
        space_id=space,
        document_id=doc2,
        state="running",
        lease_owner="dead-worker",
        lease_expires_at=past,
        ingest_run_id=None,
        attempt_count=1,
    )

    reaped = await _reap_expired(factory)
    assert reaped == 1  # only the job WITH a linked running run is reaped

    # The run is marked failed (NEVER left 'running' — dropping this write REDs).
    run_status = await _count(
        plain_dsn, "SELECT status FROM apollo_ingest_runs WHERE id=$1", run_id
    )
    assert run_status == "failed"

    # The job is now re-claimable by the FROZEN claim (lease expired).
    async with factory() as session:
        claimed = await claim_provisioning_job(
            session, lease_owner="w2", lease_seconds=300
        )
    assert claimed is not None
    assert claimed.job_id == job_id
    assert claimed.attempt_count == 2  # bumped again


# ===========================================================================
# T-DB5 — terminal-status reconciliation: a per-document error never leaves running.
# ===========================================================================
async def test_terminal_status_reconciliation_failed_run_never_running(
    committed_engine, monkeypatch
):
    plain_dsn, factory = committed_engine
    space = await _seed_space(plain_dsn)
    doc = await _seed_document(plain_dsn, space_id=space, content_hash="h1")
    await _seed_chunk(plain_dsn, document_id=doc, content="chunk text")
    run_id = await _seed_run(
        plain_dsn, space, document_id=doc, content_hash="h1", status="queued"
    )
    job_id = await _seed_job(
        plain_dsn,
        space_id=space,
        document_id=doc,
        state="running",
        ingest_run_id=run_id,
        attempt_count=1,
    )

    # Inject a stage that raises a per-document error (a generic exception is
    # caught by the orchestrator's terminal catch-all -> failed run, never running).
    async def _boom_scrape(chunks, *, chat_fn):  # noqa: ANN001
        raise RuntimeError("stage exploded")

    monkeypatch.setattr(orch, "scrape_questions", _boom_scrape)

    claimed = ClaimedJob(
        job_id=job_id,
        search_space_id=space,
        document_id=doc,
        ingest_run_id=run_id,
        attempt_count=1,
    )

    class _Metered:
        def scrape_chat_fn(self, _s):  # noqa: ANN001
            return lambda _c: "[]"

        def cheap(self, **_k):
            return "{}"

        def main(self, **_k):
            return "{}"

    async with factory() as session:
        outcome = await run_provisioning(
            session, AsyncMock(), job=claimed, metered_chat=_Metered()
        )

    assert outcome.status == "failed"
    run_status = await _count(
        plain_dsn, "SELECT status FROM apollo_ingest_runs WHERE id=$1", run_id
    )
    assert run_status == "failed"  # NEVER 'running'
    n_running = await _count(
        plain_dsn,
        "SELECT count(*) FROM apollo_ingest_runs WHERE status='running'",
    )
    assert n_running == 0


# ===========================================================================
# T-DB6 / T-DB8 — flag-OFF non-regression: enqueue never breaks the caller commit
# and leaves upload-facing columns untouched.
# ===========================================================================
async def test_flag_off_enqueue_never_breaks_caller_commit(
    committed_engine, monkeypatch
):
    plain_dsn, factory = committed_engine
    space = await _seed_space(plain_dsn)
    doc = await _seed_document(plain_dsn, space_id=space, content_hash="h1")
    # Pre-insert an OPEN job on an INDEPENDENT (committed) connection so the
    # enqueue's job insert collides on the partial-unique-index.
    await _seed_job(plain_dsn, space_id=space, document_id=doc, state="pending")

    monkeypatch.delenv("APOLLO_AUTOPROVISION_ENABLED", raising=False)

    async with factory() as session:
        # A sentinel write in the SAME session that MUST survive the commit.
        await session.execute(
            text(
                "UPDATE aita_documents SET content_hash='sentinel' WHERE id=:i"
            ).bindparams(i=doc)
        )
        result = await enqueue_provisioning_job(
            session, search_space_id=space, document_id=doc, content_hash="h1"
        )
        # The collision returns None via the begin_nested SAVEPOINT rollback...
        assert result is None
        # ...and the caller can STILL commit its other writes.
        await session.commit()

    # The sentinel write survived (the enqueue collision never wedged the commit).
    sentinel = await _count(
        plain_dsn, "SELECT content_hash FROM aita_documents WHERE id=$1", doc
    )
    assert sentinel == "sentinel"
    # Still exactly one open job (no duplicate inserted).
    n_open = await _count(
        plain_dsn,
        "SELECT count(*) FROM apollo_provisioning_jobs WHERE document_id=$1 "
        "AND state IN ('pending','running')",
        doc,
    )
    assert n_open == 1


# ===========================================================================
# T-DB7 — Procfile has the apollo-provision line.
# ===========================================================================
def test_procfile_has_apollo_provision_line():
    repo_root = Path(__file__).resolve().parents[2]
    procfile = repo_root / "Procfile"
    text_ = procfile.read_text(encoding="utf-8")
    assert "apollo-provision: python -m apollo.provision_worker" in text_
    # The original three processes remain.
    assert "web:" in text_
    assert "worker: python -m teacher_upload_worker" in text_
    assert "apollo-janitor: python -m apollo.learner_janitor_worker" in text_


# ===========================================================================
# T-DB9 — intra-job re-run skips already-scraped chunks (MUTATION PROOF c).
# ===========================================================================
async def test_intra_job_rerun_skips_scraped_chunks(committed_engine, monkeypatch):
    plain_dsn, factory = committed_engine
    space = await _seed_space(plain_dsn)
    doc = await _seed_document(plain_dsn, space_id=space, content_hash="h1")
    await _seed_chunk(plain_dsn, document_id=doc, content="chunk one")
    run_id = await _seed_run(
        plain_dsn, space, document_id=doc, content_hash="h1", status="queued"
    )
    job_id = await _seed_job(
        plain_dsn,
        space_id=space,
        document_id=doc,
        state="running",
        ingest_run_id=run_id,
        attempt_count=1,
    )

    # A deterministic single candidate; REAL write_tier1_problems runs (its frozen
    # SELECT-skip is the mutation target). Make the candidate REJECT at pairing so
    # no tag_and_mint/promote/Neo4j is needed — the proof is the Tier-1 write
    # idempotency + the orchestrator count RE-ASSIGN.
    candidate = CandidateQuestion(
        problem_text="Find P2.",
        given_values={"P1": 1.0},
        target_unknown="P2",
        difficulty="intro",
        document_id=doc,
        page=1,
        chunk_content_hash="fixedhash",
        concept_slug="provisional.inventory",
    )

    async def _scrape(chunks, *, chat_fn):  # noqa: ANN001
        return ScrapeResult(candidates=(candidate,), scraped_count=1, parse_failures=0)

    from apollo.provisioning.pairing_gate import PairingVerdict
    from apollo.provisioning.solution import ReferenceSolutionDraft

    async def _fog(db, q, *, retrieve_fn, chat_fn):  # noqa: ANN001
        return ReferenceSolutionDraft(
            solution_source="generated",
            reference_solution=[{"id": "s", "entry_type": "equation", "content": {}}],
            grounding=(),
            provenance={},
        )

    async def _vp(q, draft, *, retrieve_fn, judge_fn):  # noqa: ANN001
        return PairingVerdict(paired=False, faithful=False, confidence=0.1)

    monkeypatch.setattr(orch, "scrape_questions", _scrape)
    monkeypatch.setattr(orch, "find_or_generate", _fog)
    monkeypatch.setattr(orch, "validate_pair", _vp)

    class _Metered:
        def scrape_chat_fn(self, _s):  # noqa: ANN001
            return lambda _c: "[]"

        def cheap(self, **_k):
            return "{}"

        def main(self, **_k):
            return "{}"

    claimed = ClaimedJob(
        job_id=job_id,
        search_space_id=space,
        document_id=doc,
        ingest_run_id=run_id,
        attempt_count=1,
    )

    async def _run_once():
        async with factory() as session:
            return await run_provisioning(
                session,
                AsyncMock(),
                job=claimed,
                metered_chat=_Metered(),
                retrieve_fn=AsyncMock(return_value=()),
                embed_fn=lambda _t: [0.0],
            )

    o1 = await _run_once()
    n_tier1_after_first = await _count(
        plain_dsn,
        "SELECT count(*) FROM apollo_concept_problems WHERE problem_code=$1",
        "scrape.fixedhash",
    )
    o2 = await _run_once()
    n_tier1_after_second = await _count(
        plain_dsn,
        "SELECT count(*) FROM apollo_concept_problems WHERE problem_code=$1",
        "scrape.fixedhash",
    )

    # The second run inserts ZERO new Tier-1 rows (the frozen SELECT-skip).
    assert n_tier1_after_first == 1
    assert n_tier1_after_second == 1
    # Counts RE-ASSIGNED, not doubled (a `+=` would inflate -> REDs).
    assert o1.n_rejected == o2.n_rejected == 1
    assert o1.n_questions_scraped == o2.n_questions_scraped == 1
    run_rejected = await _count(
        plain_dsn, "SELECT n_rejected FROM apollo_ingest_runs WHERE id=$1", run_id
    )
    assert run_rejected == 1  # NOT 2


# ===========================================================================
# T-DB10 — full promote spine on committed PG through the REAL tag_and_mint +
# REAL promote (project_canon mocked). Proves: (1) the tag_and_mint chat_fn
# positional-string adapter works against a shape-faithful keyword-only
# MeteredChat.cheap (the drift bug would TypeError here); (2) the promoted row is
# RE-HOMED from the provisional concept onto the tagged concept and is selectable
# via list_problems_for_concept; (3) gate-8 dedup is non-vacuous (a second
# identical-content candidate is rejected at gate 8).
# ===========================================================================
import json as _json  # noqa: E402

_BERNOULLI_GIVENS = {
    "A1": 0.01, "A2": 0.005, "P1": 200000.0, "v1": 2.0, "rho": 1000.0,
}
_BERNOULLI_REFERENCE_SOLUTION = [
    {"id": "continuity", "step": 1, "entry_type": "equation",
     "content": {"label": "Continuity", "symbolic": "rho*A1*v1 - rho*A2*v2",
                 "variables": ["rho", "A1", "v1", "A2", "v2"]}, "depends_on": []},
    {"id": "incompressibility", "step": 2, "entry_type": "condition",
     "content": {"label": "Incompressibility", "applies_when": "density constant"},
     "depends_on": []},
    {"id": "bernoulli", "step": 3, "entry_type": "equation",
     "content": {"label": "Bernoulli", "symbolic":
                 "P1 + Rational(1,2)*rho*v1**2 + rho*g*h1 "
                 "- (P2 + Rational(1,2)*rho*v2**2 + rho*g*h2)",
                 "variables": ["P1", "rho", "v1", "g", "h1", "P2", "v2", "h2"]},
     "depends_on": ["incompressibility"]},
    {"id": "horizontal_simplification", "step": 4, "entry_type": "simplification",
     "content": {"applies_when": "h1 == h2",
                 "transformation": "rho*g*h1 and rho*g*h2 cancel"},
     "depends_on": ["bernoulli"]},
    {"id": "plan_apply_continuity", "step": 5, "entry_type": "procedure_step",
     "content": {"order": 1, "action": "use continuity to solve for v2",
                 "purpose": "obtain v2", "uses_equations": ["continuity"]},
     "depends_on": ["continuity"]},
    {"id": "plan_apply_horizontal_simplification", "step": 6,
     "entry_type": "procedure_step",
     "content": {"order": 2, "action": "set h1 == h2", "purpose": "simplify",
                 "uses_equations": ["bernoulli"]},
     "depends_on": ["bernoulli", "horizontal_simplification"]},
    {"id": "plan_solve_bernoulli_for_p2", "step": 7, "entry_type": "procedure_step",
     "content": {"order": 3, "action": "solve for P2", "purpose": "answer",
                 "uses_equations": ["bernoulli"]},
     "depends_on": ["plan_apply_continuity",
                    "plan_apply_horizontal_simplification"]},
]


def _bernoulli_candidate(*, document_id, chash):
    return CandidateQuestion(
        problem_text="Water flows through a horizontal pipe. Find the pressure P2.",
        given_values=dict(_BERNOULLI_GIVENS),
        target_unknown="P2",
        difficulty="intro",
        document_id=document_id,
        page=1,
        chunk_content_hash=chash,
        concept_slug="provisional.inventory",
    )


class _ShapeFaithfulMeteredChat:
    """Keyword-only ``.cheap``/``.main`` (mirrors the real ``MeteredChat``). The
    tag/mint stage calls its chat_fn POSITIONALLY, so the orchestrator's
    positional-string adapter is exercised; a regression would TypeError here."""

    def scrape_chat_fn(self, _system_prompt):  # noqa: ANN001
        return lambda _chunk: "[]"

    def cheap(self, *, purpose, messages, response_format=None, temperature=0.0, model=None):  # noqa: ANN001
        return _json.dumps(
            {"concept_slug": "bernoulli_principle",
             "display_name": "Bernoulli Principle", "prereqs": []}
        )

    def main(self, *, purpose, messages, response_format=None, temperature=0.0, model=None):  # noqa: ANN001
        return "{}"


async def test_full_promote_spine_rehomes_and_dedups_real_pg(
    committed_engine, monkeypatch
):
    from apollo.overseer.problem_selector import list_problems_for_concept
    from apollo.provisioning.pairing_gate import PairingVerdict
    from apollo.provisioning.solution import ReferenceSolutionDraft

    plain_dsn, factory = committed_engine
    space = await _seed_space(plain_dsn)
    doc = await _seed_document(plain_dsn, space_id=space, content_hash="h1")
    # TWO chunks -> two candidates with identical problem content (gate-8 collide).
    await _seed_chunk(plain_dsn, document_id=doc, content="chunk one")
    await _seed_chunk(plain_dsn, document_id=doc, content="chunk two")
    run_id = await _seed_run(
        plain_dsn, space, document_id=doc, content_hash="h1", status="queued"
    )
    job_id = await _seed_job(
        plain_dsn, space_id=space, document_id=doc, state="running",
        ingest_run_id=run_id, attempt_count=1,
    )

    cand_a = _bernoulli_candidate(document_id=doc, chash="bern-a")
    cand_b = _bernoulli_candidate(document_id=doc, chash="bern-b")

    async def _scrape(chunks, *, chat_fn):  # noqa: ANN001
        return ScrapeResult(candidates=(cand_a, cand_b), scraped_count=1,
                            parse_failures=0)

    async def _fog(db, q, *, retrieve_fn, chat_fn):  # noqa: ANN001
        return ReferenceSolutionDraft(
            solution_source="generated",
            reference_solution=[dict(s) for s in _BERNOULLI_REFERENCE_SOLUTION],
            grounding=(), provenance={},
        )

    async def _vp(q, draft, *, retrieve_fn, judge_fn):  # noqa: ANN001
        return PairingVerdict(paired=True, faithful=True, confidence=1.0)

    async def _noop_canon(db, neo, *, search_space_id, concept_id):  # noqa: ANN001
        return None

    monkeypatch.setattr(orch, "scrape_questions", _scrape)
    monkeypatch.setattr(orch, "find_or_generate", _fog)
    monkeypatch.setattr(orch, "validate_pair", _vp)
    # Mock project_canon on the promote module surface (the frozen 3C1 MERGE).
    promote_mod = sys.modules["apollo.provisioning.promote"]
    monkeypatch.setattr(promote_mod, "project_canon", _noop_canon)

    claimed = ClaimedJob(
        job_id=job_id, search_space_id=space, document_id=doc,
        ingest_run_id=run_id, attempt_count=1,
    )

    async with factory() as session:
        outcome = await run_provisioning(
            session, AsyncMock(), job=claimed,
            metered_chat=_ShapeFaithfulMeteredChat(),
            retrieve_fn=AsyncMock(return_value=()),
            embed_fn=lambda _t: [0.0] * 8,
        )

    # One promotes (re-homed to the tagged concept); the duplicate hits gate 8.
    assert outcome.status == "succeeded"
    assert outcome.n_promoted == 1
    assert outcome.n_rejected == 1

    # The promoted row was RE-HOMED off the provisional concept onto the tagged one
    # and is teachable (tier=2, selectable). Resolve the tagged concept id.
    tagged_id = await _count(
        plain_dsn,
        "SELECT c.id FROM apollo_concepts c JOIN apollo_subjects s "
        "ON c.subject_id = s.id WHERE s.search_space_id=$1 AND c.slug=$2",
        space, "bernoulli_principle",
    )
    assert tagged_id is not None
    n_tier2_on_tagged = await _count(
        plain_dsn,
        "SELECT count(*) FROM apollo_concept_problems WHERE concept_id=$1 AND tier=2",
        tagged_id,
    )
    assert n_tier2_on_tagged == 1  # the re-homed promoted row

    # The student selector can reach it under the tagged concept.
    async with factory() as session:
        teachable = await list_problems_for_concept(session, concept_id=tagged_id)
    assert len(teachable) == 1

    # The gate-8 rejection is recorded with failed_gate=8 (non-vacuous dedup).
    failed_gate = await _count(
        plain_dsn,
        "SELECT failed_gate FROM apollo_rejected_problems "
        "WHERE ingest_run_id=$1 AND rejected_stage='promotion_lint'",
        run_id,
    )
    assert failed_gate == 8
