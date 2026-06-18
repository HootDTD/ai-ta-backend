"""WU-4B3 — REAL-PG gate: the §6.11 persistence-touching corpus end-to-end.

Drives the persistence-touching corpus fixtures (parser-miss, high-unresolved
abstention, the §6.9 Bernoulli capstone) through the FULL chain
``build_audited_grade -> convert_findings_to_events -> persist_comparison_run``
on the real pgvector pg16 ``db_session``, then asserts the persisted run +
findings AND the produced events. Proves the resolve->grade->audit->event->persist
chain end-to-end on real PG. Deterministic resolution + injected ``audit_fn`` —
NO live LLM. MUST RUN GREEN (not skip) with Docker up.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from apollo.grading.events import convert_findings_to_events
from apollo.grading.fixtures.corpus import PERSISTING_CORPUS
from apollo.grading.normalization_confidence import compute_normalization_confidence
from apollo.grading.persistence import persist_comparison_run
from apollo.grading.reference_hash import reference_graph_hash
from apollo.persistence.models import (
    ApolloSession,
    GraphComparisonFinding,
    GraphComparisonRun,
    ProblemAttempt,
)
from database.models import SearchSpace

pytestmark = pytest.mark.integration

_USER_ID = "33333333-3333-3333-3333-333333333333"

_BY_NAME = {f.name: f for f in PERSISTING_CORPUS}


async def _seed_attempt(db) -> tuple[int, int]:
    slug = f"course-{uuid.uuid4().hex[:8]}"
    space = SearchSpace(name=f"Course {slug}", slug=slug, subject_name="Physics")
    db.add(space)
    await db.flush()
    session = ApolloSession(user_id=_USER_ID, search_space_id=space.id)
    db.add(session)
    await db.flush()
    attempt = ProblemAttempt(session_id=session.id, problem_id="p1", difficulty="easy")
    db.add(attempt)
    await db.flush()
    return attempt.id, space.id


async def _persist_fixture(db, fixture) -> tuple[int, object, tuple]:
    """Run the chain + persist; return (run_id, audited, events)."""
    attempt_id, search_space_id = await _seed_attempt(db)
    audited, events = fixture.run_chain()
    norm_conf = compute_normalization_confidence(audited, fixture.resolution)
    ref_hash = reference_graph_hash(fixture.reference_graph)
    run_id = await persist_comparison_run(
        db,
        attempt_id=attempt_id,
        user_id=_USER_ID,
        search_space_id=search_space_id,
        grade=audited.grade,
        audited=audited,
        normalization_confidence=norm_conf,
        reference_graph_hash=ref_hash,
    )
    return run_id, audited, events


async def _findings_for_run(db, run_id: int):
    return (
        (
            await db.execute(
                select(GraphComparisonFinding).where(
                    GraphComparisonFinding.run_id == run_id
                )
            )
        )
        .scalars()
        .all()
    )


def test_persisting_corpus_is_the_three_named_rows():
    """Guard: the persistence-touching corpus is exactly the 3 spec rows."""
    assert set(_BY_NAME) == {
        "parser_misses_key_sentence",
        "high_unresolved_abstains",
        "bernoulli_capstone",
    }


async def test_parser_miss_persists_upgraded_covered(db_session):
    fixture = _BY_NAME["parser_misses_key_sentence"]
    run_id, audited, events = await _persist_fixture(db_session, fixture)

    rows = await _findings_for_run(db_session, run_id)
    covered = [r for r in rows if r.finding_kind == "covered_node"]
    assert len(covered) == 1
    upgraded = covered[0]
    # audit-upgraded covered carries the span + AUDIT_UPGRADE_MESSAGE at <=0.75.
    from apollo.grading.audited_grade import AUDIT_UPGRADE_MESSAGE

    assert upgraded.message == AUDIT_UPGRADE_MESSAGE
    assert upgraded.confidence <= 0.75
    assert upgraded.evidence_spans == ["the student said the flow is steady"]
    # NO false missing row persisted for that key.
    assert not any(r.finding_kind == "missing_node" for r in rows)
    # the produced event is a covered <=0.75 (no false missing).
    covered_events = [e for e in events if e.event_kind.value == "covered"]
    assert len(covered_events) == 1
    assert covered_events[0].score <= 0.75
    assert not any(e.event_kind.value == "missing" for e in events)


async def test_high_unresolved_persists_run_no_events(db_session):
    fixture = _BY_NAME["high_unresolved_abstains"]
    run_id, audited, events = await _persist_fixture(db_session, fixture)

    run = (
        await db_session.execute(
            select(GraphComparisonRun).where(GraphComparisonRun.id == run_id)
        )
    ).scalar_one()
    assert run.abstained is True
    assert "unresolved_rate_above_threshold" in run.abstention_reasons
    # findings persisted (persist-ALWAYS) ...
    count = (
        await db_session.execute(
            select(func.count())
            .select_from(GraphComparisonFinding)
            .where(GraphComparisonFinding.run_id == run_id)
        )
    ).scalar_one()
    assert count > 0
    # ... but NO events (the §6.11 binding for this row).
    assert events == ()


async def test_bernoulli_capstone_persisted(db_session):
    fixture = _BY_NAME["bernoulli_capstone"]
    run_id, audited, events = await _persist_fixture(db_session, fixture)

    rows = await _findings_for_run(db_session, run_id)
    kinds = sorted(r.finding_kind for r in rows)
    # 3 covered (2 plain + 1 audit-upgraded) + 1 surviving missing.
    assert kinds == ["covered_node", "covered_node", "covered_node", "missing_node"]

    event_kinds = sorted(e.event_kind.value for e in events)
    assert event_kinds == ["covered", "covered", "covered", "missing"]

    # the run row itself persisted with the score core's values.
    run = (
        await db_session.execute(
            select(GraphComparisonRun).where(GraphComparisonRun.id == run_id)
        )
    ).scalar_one()
    assert run.abstained is False
    assert run.reference_graph_hash.startswith("refhash-v1:")
