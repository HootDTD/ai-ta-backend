"""WU-3B2g Step 1 — enqueue idempotency unit tests (savepoint ``db_session``).

These prove the content-hash short-circuit (the SELECT-then-skip on a
``status='succeeded'`` run for ``(document_id, content_hash)``) and the happy
insert (one ``apollo_ingest_runs`` row + one ``apollo_provisioning_jobs`` row,
FK-linked). The partial-unique-index collision case is migration-only and is
proved in ``tests/database/test_apollo_provisioning_orchestration.py`` (the
real-PG file) — ``create_all`` does NOT build that index, so it cannot be
exercised here.

Tier-1 — NO network, NO LLM. The savepoint ``db_session`` fixture is a real
pgvector session; tests Docker-skip cleanly when the daemon is down but the
WU-3B2g gate requires them GREEN-not-skipped.
"""

from __future__ import annotations

import sys

from sqlalchemy import func, select

from apollo.persistence.models import IngestRun, ProvisioningJob
from apollo.provisioning import enqueue_provisioning_job
from database.models import SearchSpace

_enqueue_mod = sys.modules["apollo.provisioning.enqueue"]

# pytest.ini sets asyncio_mode = auto.


async def _seed_space(db, *, slug: str) -> int:
    space = SearchSpace(name=f"Course {slug}", slug=slug, subject_name="Physics")
    db.add(space)
    await db.flush()
    return int(space.id)


async def _count_runs(db, *, document_id: int) -> int:
    return (
        await db.execute(
            select(func.count(IngestRun.id)).where(IngestRun.document_id == document_id)
        )
    ).scalar_one()


async def _count_jobs(db, *, document_id: int) -> int:
    return (
        await db.execute(
            select(func.count(ProvisioningJob.id)).where(ProvisioningJob.document_id == document_id)
        )
    ).scalar_one()


# --------------------------------------------------------------------------- #
# T-EQ1 — happy insert
# --------------------------------------------------------------------------- #
async def test_enqueue_inserts_run_and_job(db_session):
    space = await _seed_space(db_session, slug="eq1")

    job_id = await enqueue_provisioning_job(
        db_session,
        search_space_id=space,
        document_id=7,
        content_hash="h",
    )

    assert job_id is not None
    job = await db_session.get(ProvisioningJob, job_id)
    assert job is not None
    assert job.state == "pending"
    assert job.document_id == 7
    assert job.search_space_id == space
    # The FK link: the job points at a queued run for this document.
    run = await db_session.get(IngestRun, job.ingest_run_id)
    assert run is not None
    assert run.status == "queued"
    assert run.document_id == 7
    assert run.content_hash == "h"
    assert run.search_space_id == space


# --------------------------------------------------------------------------- #
# T-EQ2 — content-hash short-circuit (MUTATION-DISCRIMINATING for §2a)
# --------------------------------------------------------------------------- #
async def test_enqueue_short_circuits_on_succeeded_run(db_session):
    space = await _seed_space(db_session, slug="eq2")
    # Pre-seed a SUCCEEDED run for (document_id=7, content_hash='h').
    db_session.add(
        IngestRun(
            search_space_id=space,
            document_id=7,
            content_hash="h",
            status="succeeded",
        )
    )
    await db_session.flush()

    runs_before = await _count_runs(db_session, document_id=7)
    jobs_before = await _count_jobs(db_session, document_id=7)

    result = await enqueue_provisioning_job(
        db_session,
        search_space_id=space,
        document_id=7,
        content_hash="h",
    )

    assert result is None  # short-circuited
    # ZERO new rows: removing the succeeded-run SELECT -> a job appears -> REDs.
    assert await _count_runs(db_session, document_id=7) == runs_before
    assert await _count_jobs(db_session, document_id=7) == jobs_before


# --------------------------------------------------------------------------- #
# T-EQ3 — no short-circuit on failed / different hash
# --------------------------------------------------------------------------- #
async def test_enqueue_does_not_short_circuit_on_failed_or_different_hash(db_session):
    space = await _seed_space(db_session, slug="eq3")
    # A FAILED run for the same (document_id, content_hash) must NOT short-circuit.
    db_session.add(
        IngestRun(
            search_space_id=space,
            document_id=7,
            content_hash="h",
            status="failed",
        )
    )
    # A SUCCEEDED run for a DIFFERENT content_hash must NOT short-circuit either.
    db_session.add(
        IngestRun(
            search_space_id=space,
            document_id=7,
            content_hash="other-hash",
            status="succeeded",
        )
    )
    await db_session.flush()

    job_id = await enqueue_provisioning_job(
        db_session,
        search_space_id=space,
        document_id=7,
        content_hash="h",
    )

    assert job_id is not None  # a changed/failed re-upload re-enqueues
    job = await db_session.get(ProvisioningJob, job_id)
    assert job.state == "pending"


# --------------------------------------------------------------------------- #
# T-EQ4 — None content hash (legacy doc) enqueues (boundary)
# --------------------------------------------------------------------------- #
async def test_enqueue_none_content_hash_enqueues(db_session):
    space = await _seed_space(db_session, slug="eq4")

    job_id = await enqueue_provisioning_job(
        db_session,
        search_space_id=space,
        document_id=9,
        content_hash=None,
    )

    assert job_id is not None  # cannot short-circuit a None hash
    job = await db_session.get(ProvisioningJob, job_id)
    run = await db_session.get(IngestRun, job.ingest_run_id)
    assert run.content_hash is None
    assert run.status == "queued"


# --------------------------------------------------------------------------- #
# T-EQ5 — defensive backstop: a failure inside the enqueue NEVER raises out
# (the §14 byte-identical-upload non-regression — a degraded env must not take
# down a teacher upload). A non-Integrity error is swallowed via the savepoint.
# --------------------------------------------------------------------------- #
async def test_enqueue_swallows_unexpected_error_and_returns_none(db_session, monkeypatch):
    space = await _seed_space(db_session, slug="eq5")

    class _Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("simulated degraded environment (e.g. missing table)")

    # IngestRun construction blows up mid-enqueue (simulating a degraded env).
    monkeypatch.setattr(_enqueue_mod, "IngestRun", _Boom)

    # A sentinel write in the SAME session that MUST survive (the enqueue failure
    # rolled back only its own savepoint, not the caller's other writes).
    sentinel = SearchSpace(name="Sentinel", slug="eq5-sentinel", subject_name="P")
    db_session.add(sentinel)
    await db_session.flush()

    result = await enqueue_provisioning_job(
        db_session,
        search_space_id=space,
        document_id=42,
        content_hash="h",
    )

    assert result is None  # no-op, did NOT raise
    # The caller's other writes are intact and committable.
    await db_session.commit()
    survived = await db_session.get(SearchSpace, sentinel.id)
    assert survived is not None
    # No provisioning rows were left behind.
    assert await _count_jobs(db_session, document_id=42) == 0
    assert await _count_runs(db_session, document_id=42) == 0
