"""ORM-parity tests for the WU-3B2a auto-provisioning models (migration 030).

These exercise the ORM additions on the ``db_session`` (``create_all``-built)
schema so the ORM edits in ``apollo/persistence/models.py`` are MEASURED by
diff-cover (run with ``--cov=.``): the six ``ProblemRecord``/``KGEntity`` column
adds and the retained authored-ingest models (``IngestRun``/``DedupDecision``/
``IngestError``). The worker-only queue and rejection models were removed.

Postgres-only DDL semantics (the CHECK constraints, the partial-unique-index
collapse, FK cascades, both backfills) are asserted for real in
``tests/database/test_apollo_autoprovisioning_migration.py``. This file asserts
the ORM round-trips the typed columns + defaults the ``db_session`` schema gives.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from apollo.persistence.models import (
    DedupDecision,
    IngestError,
    IngestPageEvidence,
    IngestRun,
    KGEntity,
)
from apollo.persistence.models import (
    Problem as ProblemRecord,
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


def test_ingest_models_map_to_internal_targets_with_document_fks():
    assert IngestRun.__table__.fullname == "internal.content_ingest_runs"
    assert IngestError.__table__.fullname == "internal.content_ingest_errors"
    assert IngestPageEvidence.__table__.fullname == "internal.ingest_page_evidence"
    assert DedupDecision.__table__.fullname == "internal.dedup_decisions"

    run_document_fks = {fk.target_fullname for fk in IngestRun.document_id.property.columns[0].foreign_keys}
    evidence_document_fks = {
        fk.target_fullname
        for fk in IngestPageEvidence.document_id.property.columns[0].foreign_keys
    }
    assert run_document_fks == {"app.documents.id"}
    assert evidence_document_fks == {"app.documents.id"}


async def test_orm_concept_problem_has_tier_columns(db_session):
    """Round-trip the new ProblemRecord columns; assert the ORM default tier=2
    and provenance={} when omitted (the seed-helper/ORM reality is teachable)."""
    sid, cid = await _space_and_concept(db_session)
    explicit = ProblemRecord.from_inventory_payload(
        {"id": "orm_explicit", "difficulty": "intro", "problem_text": ""},
        course_id=sid,
        concept_id=cid,
        tier=2,
        solution_source="authored",
        provenance={"document_id": 1},
    )
    db_session.add(explicit)
    await db_session.flush()
    db_session.expire(explicit)
    reloaded = (
        await db_session.execute(
            select(ProblemRecord).where(ProblemRecord.problem_code == "orm_explicit")
        )
    ).scalar_one()
    assert reloaded.tier == 2
    assert reloaded.solution_source == "authored"
    assert reloaded.provenance == {"document_id": 1}
    assert reloaded.course_id == sid
    assert reloaded.quarantined_at is None

    # Omitting tier -> ORM default 2; omitting provenance -> {}.
    defaulted = ProblemRecord.from_inventory_payload(
        {"id": "orm_defaulted", "difficulty": "intro", "problem_text": ""},
        course_id=sid,
        concept_id=cid,
    )
    db_session.add(defaulted)
    await db_session.flush()
    db_session.expire(defaulted)
    reloaded2 = (
        await db_session.execute(
            select(ProblemRecord).where(ProblemRecord.problem_code == "orm_defaulted")
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
    run = IngestRun(search_space_id=sid, document_id=None)
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
    assert reloaded.dedup_total_candidates == 0
    assert reloaded.dedup_exact_merges == 0
    assert reloaded.dedup_embedding_merges == 0
    assert reloaded.dedup_embedding_distinct == 0
    assert reloaded.dedup_embedding_merge_ratio == 0
    assert reloaded.dedup_details == {}
    assert reloaded.llm_calls == 0
    assert reloaded.llm_tokens_in == 0
    assert reloaded.llm_tokens_out == 0
    assert reloaded.llm_cost_usd == 0
    assert reloaded.created_at is not None


async def test_orm_rejected_dedup_error_roundtrip(db_session):
    """DedupDecision / IngestError round-trip their typed columns."""
    sid, cid = await _space_and_concept(db_session)
    run = IngestRun(search_space_id=sid, document_id=None)
    db_session.add(run)
    await db_session.flush()
    run_id = run.id

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
    db_session.add_all([dedup, error])
    await db_session.flush()
    dedup_id, error_id = dedup.id, error.id
    db_session.expire(dedup)
    db_session.expire(error)

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
