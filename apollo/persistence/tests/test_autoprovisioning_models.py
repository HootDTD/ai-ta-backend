"""ORM-parity tests for the WU-3B2a auto-provisioning models (migration 030).

These exercise the ORM additions on the ``db_session`` (``create_all``-built)
schema so the ORM edits in ``apollo/persistence/models.py`` are MEASURED by
diff-cover (run with ``--cov=.``): the six ``ConceptProblem``/``KGEntity`` column
adds and the five new model classes (``IngestRun``/``ProvisioningJob``/
``RejectedProblem``/``DedupDecision``/``IngestError``).

Postgres-only DDL semantics (the CHECK constraints, the partial-unique-index
collapse, FK cascades, both backfills) are asserted for real in
``tests/database/test_apollo_autoprovisioning_migration.py``. This file asserts
the ORM round-trips the typed columns + defaults the ``db_session`` schema gives.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from apollo.persistence.models import (
    ConceptProblem,
    DedupDecision,
    IngestError,
    IngestRun,
    KGEntity,
    ProvisioningJob,
    RejectedProblem,
)
from apollo.subjects.tests._curriculum_fixtures import seed_concept, seed_search_space

pytestmark = pytest.mark.integration


async def _space_and_concept(db_session) -> tuple[int, int]:
    """A minimal course chain (search_space -> subject -> concept) for FK targets."""
    sid = await seed_search_space(db_session)
    cid = await seed_concept(
        db_session, search_space_id=sid, subject_slug="s_orm", concept_slug="c_orm"
    )
    return sid, cid


async def test_orm_concept_problem_has_tier_columns(db_session):
    """Round-trip the new ConceptProblem columns; assert the ORM default tier=2
    and provenance={} when omitted (the seed-helper/ORM reality is teachable)."""
    sid, cid = await _space_and_concept(db_session)
    explicit = ConceptProblem(
        concept_id=cid,
        problem_code="orm_explicit",
        difficulty="intro",
        payload={"id": "orm_explicit", "difficulty": "intro"},
        tier=2,
        solution_source="authored",
        provenance={"document_id": 1},
        search_space_id=sid,
    )
    db_session.add(explicit)
    await db_session.flush()
    db_session.expire(explicit)
    reloaded = (
        await db_session.execute(
            select(ConceptProblem).where(ConceptProblem.problem_code == "orm_explicit")
        )
    ).scalar_one()
    assert reloaded.tier == 2
    assert reloaded.solution_source == "authored"
    assert reloaded.provenance == {"document_id": 1}
    assert reloaded.search_space_id == sid
    assert reloaded.quarantined_at is None

    # Omitting tier -> ORM default 2; omitting provenance -> {}.
    defaulted = ConceptProblem(
        concept_id=cid,
        problem_code="orm_defaulted",
        difficulty="intro",
        payload={"id": "orm_defaulted", "difficulty": "intro"},
    )
    db_session.add(defaulted)
    await db_session.flush()
    db_session.expire(defaulted)
    reloaded2 = (
        await db_session.execute(
            select(ConceptProblem).where(ConceptProblem.problem_code == "orm_defaulted")
        )
    ).scalar_one()
    assert reloaded2.tier == 2
    assert reloaded2.provenance == {}
    assert reloaded2.solution_source is None


async def test_orm_kg_entity_scope_summary_roundtrip(db_session):
    """scope_summary persists when set, NULL when omitted."""
    _sid, cid = await _space_and_concept(db_session)
    ent = KGEntity(
        concept_id=cid,
        canonical_key="k_scope",
        kind="concept",
        display_name="Pressure",
        scope_summary="pressure-velocity relation",
    )
    db_session.add(ent)
    await db_session.flush()
    db_session.expire(ent)
    reloaded = (
        await db_session.execute(select(KGEntity).where(KGEntity.canonical_key == "k_scope"))
    ).scalar_one()
    assert reloaded.scope_summary == "pressure-velocity relation"

    ent2 = KGEntity(
        concept_id=cid,
        canonical_key="k_noscope",
        kind="concept",
        display_name="Velocity",
    )
    db_session.add(ent2)
    await db_session.flush()
    db_session.expire(ent2)
    reloaded2 = (
        await db_session.execute(select(KGEntity).where(KGEntity.canonical_key == "k_noscope"))
    ).scalar_one()
    assert reloaded2.scope_summary is None


async def test_orm_ingest_run_defaults(db_session):
    """IngestRun status/counters/cost default on the ORM-built schema."""
    sid, _cid = await _space_and_concept(db_session)
    run = IngestRun(search_space_id=sid, document_id=1)
    db_session.add(run)
    await db_session.flush()
    run_id = run.id
    db_session.expire(run)
    reloaded = (
        await db_session.execute(select(IngestRun).where(IngestRun.id == run_id))
    ).scalar_one()
    assert reloaded.status == "queued"
    assert reloaded.n_questions_scraped == 0
    assert reloaded.n_promoted == 0
    assert reloaded.n_rejected == 0
    assert reloaded.n_dedup_merged == 0
    assert reloaded.llm_calls == 0
    assert reloaded.llm_tokens_in == 0
    assert reloaded.llm_tokens_out == 0
    assert reloaded.llm_cost_usd == 0
    assert reloaded.created_at is not None


async def test_orm_provisioning_job_roundtrip(db_session):
    """ProvisioningJob defaults (state='pending', attempt_count=0) + the lease
    fields round-trip when set."""
    sid, _cid = await _space_and_concept(db_session)
    job = ProvisioningJob(search_space_id=sid, document_id=1)
    db_session.add(job)
    await db_session.flush()
    job_id = job.id
    db_session.expire(job)
    reloaded = (
        await db_session.execute(select(ProvisioningJob).where(ProvisioningJob.id == job_id))
    ).scalar_one()
    assert reloaded.state == "pending"
    assert reloaded.attempt_count == 0
    assert reloaded.ingest_run_id is None

    run = IngestRun(search_space_id=sid, document_id=2)
    db_session.add(run)
    await db_session.flush()
    run_id = run.id
    leased = ProvisioningJob(
        search_space_id=sid,
        document_id=2,
        lease_owner="worker-1",
        ingest_run_id=run_id,
    )
    db_session.add(leased)
    await db_session.flush()
    leased_id = leased.id
    db_session.expire(leased)
    reloaded2 = (
        await db_session.execute(select(ProvisioningJob).where(ProvisioningJob.id == leased_id))
    ).scalar_one()
    assert reloaded2.lease_owner == "worker-1"
    assert reloaded2.ingest_run_id == run_id


async def test_orm_rejected_dedup_error_roundtrip(db_session):
    """RejectedProblem / DedupDecision / IngestError round-trip the typed columns
    (incl. _JSONType payload/context as dict and similarity REAL as float)."""
    sid, cid = await _space_and_concept(db_session)
    run = IngestRun(search_space_id=sid, document_id=1)
    db_session.add(run)
    await db_session.flush()
    run_id = run.id

    rejected = RejectedProblem(
        ingest_run_id=run_id,
        search_space_id=sid,
        concept_id=cid,
        failed_gate=3,
        rejected_stage="promotion_lint",
        diagnostic="missing reference solution",
        payload={"q": "x"},
    )
    dedup = DedupDecision(
        ingest_run_id=run_id,
        search_space_id=sid,
        concept_id=cid,
        candidate_key="bernoulli/eq",
        method="embedding",
        similarity=0.91,
        verdict="merged",
    )
    error = IngestError(
        ingest_run_id=run_id,
        search_space_id=sid,
        stage="scrape",
        error_class="TimeoutError",
        context={"url": "x"},
    )
    db_session.add_all([rejected, dedup, error])
    await db_session.flush()
    rejected_id, dedup_id, error_id = rejected.id, dedup.id, error.id
    db_session.expire(rejected)
    db_session.expire(dedup)
    db_session.expire(error)

    r = (
        await db_session.execute(select(RejectedProblem).where(RejectedProblem.id == rejected_id))
    ).scalar_one()
    assert r.failed_gate == 3
    assert r.rejected_stage == "promotion_lint"
    assert r.payload == {"q": "x"}

    d = (
        await db_session.execute(select(DedupDecision).where(DedupDecision.id == dedup_id))
    ).scalar_one()
    assert d.method == "embedding"
    assert d.verdict == "merged"
    assert d.similarity == pytest.approx(0.91, rel=1e-3)

    e = (
        await db_session.execute(select(IngestError).where(IngestError.id == error_id))
    ).scalar_one()
    assert e.stage == "scrape"
    assert e.error_class == "TimeoutError"
    assert e.context == {"url": "x"}
