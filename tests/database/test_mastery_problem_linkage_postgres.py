"""GEN-5 real-Postgres gates for per-item mastery-event linkage."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from apollo.conftest import TEST_USER_ID
from apollo.grading.audited_grade import AuditedGrade
from apollo.graph_compare.findings import Finding, FindingKind
from apollo.handlers.done_grading import ShadowGradeResult
from apollo.learner_model.persistence import persist_learner_update
from apollo.persistence.models import (
    ApolloSession,
    Concept,
    ConceptProblem,
    GradingArtifact,
    KGEntity,
    MasteryEvent,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
    Subject,
)
from apollo.persistence.problem_linkage import resolve_concept_problem_id
from apollo.projections.mastery import update_mastery_from_artifact
from apollo.resolution.result import ResolutionResult
from database.models import SearchSpace

pytestmark = pytest.mark.integration

_ENTITY_KEY = "eq.linked"


async def _seed_course_with_entity(db) -> tuple[int, int, int]:
    suffix = uuid.uuid4().hex[:10]
    space = SearchSpace(name=f"GEN-5 {suffix}", slug=f"gen5-{suffix}", subject_name="Physics")
    db.add(space)
    await db.flush()
    subject = Subject(
        slug=f"subject-{suffix}",
        display_name="GEN-5",
        search_space_id=space.id,
    )
    db.add(subject)
    await db.flush()
    concept = Concept(
        subject_id=subject.id,
        slug=f"concept-{suffix}",
        display_name="GEN-5 concept",
    )
    db.add(concept)
    await db.flush()
    entity = KGEntity(
        concept_id=concept.id,
        canonical_key=_ENTITY_KEY,
        kind="equation",
        display_name="Linked entity",
        payload={},
        aliases=[],
    )
    db.add(entity)
    await db.flush()
    return int(space.id), int(concept.id), int(entity.id)


async def _seed_session_attempt(
    db, *, search_space_id: int, concept_id: int, problem_code: str
) -> tuple[ApolloSession, ProblemAttempt]:
    session = ApolloSession(
        user_id=TEST_USER_ID,
        search_space_id=search_space_id,
        concept_id=concept_id,
        status=SessionStatus.ended.value,
        phase=SessionPhase.REPORT.value,
        current_problem_id=problem_code,
    )
    db.add(session)
    await db.flush()
    attempt = ProblemAttempt(
        session_id=session.id,
        problem_id=problem_code,
        difficulty="intro",
        result="graded",
    )
    db.add(attempt)
    await db.flush()
    return session, attempt


async def _seed_problem(
    db,
    *,
    concept_id: int,
    search_space_id: int,
    problem_code: str,
    tier: int,
    quarantined_at: datetime | None = None,
) -> ConceptProblem:
    problem = ConceptProblem(
        concept_id=concept_id,
        search_space_id=search_space_id,
        problem_code=problem_code,
        difficulty="intro",
        payload={"id": problem_code},
        tier=tier,
        solution_source="authored",
        provenance={},
        quarantined_at=quarantined_at,
    )
    db.add(problem)
    await db.flush()
    return problem


def _shadow() -> ShadowGradeResult:
    audited = AuditedGrade(
        grade=object(),
        findings=(
            Finding(
                kind=FindingKind.COVERED_NODE,
                canonical_key=_ENTITY_KEY,
                student_node_ids=("student-node",),
                score=1.0,
                confidence=0.9,
            ),
        ),
        abstention_reasons=(),
        abstained=False,
        suppressed_event_kinds=frozenset(),
        alias_candidates=(),
    )
    return ShadowGradeResult(
        run_id=1,
        grade=object(),
        audited=audited,
        normalization_confidence=0.8,
        reference_graph_hash="refhash-v1:gen5",
        opposes_map={},
        turn_order={},
        graph_sim_rubric={},
        calibration=object(),
        diagnostic=object(),
        resolution=ResolutionResult(resolved=(), tier_counts={}, llm_calls=0),
    )


async def _persist_path_a(db, *, session, attempt, entity_id: int) -> MasteryEvent:
    await persist_learner_update(
        db,
        sess=session,
        attempt=attempt,
        shadow=_shadow(),
        done_ts=datetime(2026, 7, 12, 12, 0, tzinfo=UTC),
        parser_confidence=0.9,
        canon_key_by_canonical_key={_ENTITY_KEY: entity_id},
    )
    return (
        await db.execute(select(MasteryEvent).where(MasteryEvent.attempt_id == attempt.id))
    ).scalar_one()


def _artifact(*, attempt_id: int, search_space_id: int, concept_id: int) -> GradingArtifact:
    return GradingArtifact(
        attempt_id=attempt_id,
        role="canonical",
        grader_used="graph",
        user_id=TEST_USER_ID,
        search_space_id=search_space_id,
        concept_id=concept_id,
        problem_id="artifact-display-id",
        versions={"grader": "v1"},
        node_ledger=[
            {
                "canonical_key": _ENTITY_KEY,
                "status": "credited",
                "method": None,
                "confidence": 0.9,
                "evidence_span": "",
            }
        ],
        edge_ledger=[],
        misconceptions=[],
        clarification_trace=[],
        scores={"composite": 0.75},
        abstention={"normalization_confidence": 0.8},
    )


async def test_path_a_stamps_tier_two_problem(db_session):
    sid, cid, entity_id = await _seed_course_with_entity(db_session)
    session, attempt = await _seed_session_attempt(
        db_session,
        search_space_id=sid,
        concept_id=cid,
        problem_code="p-linked",
    )
    tier_one = await _seed_problem(
        db_session,
        concept_id=cid,
        search_space_id=sid,
        problem_code="p-linked",
        tier=1,
    )
    tier_two = await _seed_problem(
        db_session,
        concept_id=cid,
        search_space_id=sid,
        problem_code="p-linked",
        tier=2,
    )

    event = await _persist_path_a(
        db_session,
        session=session,
        attempt=attempt,
        entity_id=entity_id,
    )

    assert event.concept_problem_id == tier_two.id
    assert event.concept_problem_id != tier_one.id


async def test_resolver_excludes_quarantine_and_uses_newest_id_tiebreak(db_session):
    sid, cid, _ = await _seed_course_with_entity(db_session)
    older_live = await _seed_problem(
        db_session,
        concept_id=cid,
        search_space_id=sid,
        problem_code="p-order",
        tier=2,
    )
    newer_live = await _seed_problem(
        db_session,
        concept_id=cid,
        search_space_id=sid,
        problem_code="p-order",
        tier=2,
    )
    await _seed_problem(
        db_session,
        concept_id=cid,
        search_space_id=sid,
        problem_code="p-order",
        tier=2,
        quarantined_at=datetime(2026, 7, 12, tzinfo=UTC),
    )

    resolved = await resolve_concept_problem_id(
        db_session,
        concept_id=cid,
        problem_code="p-order",
    )

    assert resolved == newer_live.id
    assert resolved != older_live.id


async def test_path_a_unresolvable_code_writes_null_and_supersede_restamps(db_session):
    sid, cid, entity_id = await _seed_course_with_entity(db_session)
    session, attempt = await _seed_session_attempt(
        db_session,
        search_space_id=sid,
        concept_id=cid,
        problem_code="legacy-code",
    )

    first = await _persist_path_a(
        db_session,
        session=session,
        attempt=attempt,
        entity_id=entity_id,
    )
    assert first.concept_problem_id is None

    linked = await _seed_problem(
        db_session,
        concept_id=cid,
        search_space_id=sid,
        problem_code="legacy-code",
        tier=2,
    )
    first_event_id = int(first.id)
    db_session.expire(first)

    replacement = await _persist_path_a(
        db_session,
        session=session,
        attempt=attempt,
        entity_id=entity_id,
    )

    assert replacement.id != first_event_id
    assert replacement.concept_problem_id == linked.id


async def test_path_b_stamps_problem_from_attempt_session_join(db_session):
    sid, cid, _ = await _seed_course_with_entity(db_session)
    _, attempt = await _seed_session_attempt(
        db_session,
        search_space_id=sid,
        concept_id=cid,
        problem_code="p-composite",
    )
    linked = await _seed_problem(
        db_session,
        concept_id=cid,
        search_space_id=sid,
        problem_code="p-composite",
        tier=2,
    )
    artifact = _artifact(attempt_id=int(attempt.id), search_space_id=sid, concept_id=cid)
    db_session.add(artifact)
    await db_session.flush()

    await update_mastery_from_artifact(db_session, artifact_row=artifact)

    event = (
        await db_session.execute(select(MasteryEvent).where(MasteryEvent.attempt_id == attempt.id))
    ).scalar_one()
    assert event.concept_problem_id == linked.id


async def test_path_b_unresolvable_code_writes_null_event(db_session):
    sid, cid, _ = await _seed_course_with_entity(db_session)
    _, attempt = await _seed_session_attempt(
        db_session,
        search_space_id=sid,
        concept_id=cid,
        problem_code="missing-composite",
    )
    artifact = _artifact(attempt_id=int(attempt.id), search_space_id=sid, concept_id=cid)
    db_session.add(artifact)
    await db_session.flush()

    await update_mastery_from_artifact(db_session, artifact_row=artifact)

    event = (
        await db_session.execute(select(MasteryEvent).where(MasteryEvent.attempt_id == attempt.id))
    ).scalar_one()
    assert event.concept_problem_id is None
