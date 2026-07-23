"""Campaign-plan Task B2 — real-PG behavioral gate for
``apollo.projections.mastery.update_mastery_from_artifact``.

Builds a real ``apollo_kg_entities`` row under a seeded course/concept, a
``GradingRun`` row referencing it by ``canonical_key`` in its ``node_ledger``
(constructed directly rather than through ``write_artifacts`` — this module
tests the PROJECTION, not the artifact writer), and asserts the appended
``MasteryEvent`` + upserted ``LearnerState`` rows.

``GradingRun`` (DB-14/A7 artifacts-only merge onto ``internal.grading_runs``)
has a NOT NULL FK ``problem_id -> app.problems.id`` (ON DELETE RESTRICT), so
every seeded attempt here also seeds a real problem row via the shared
curriculum fixtures rather than a bare literal id."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from apollo.persistence.models import (
    GradingRun,
    LearnerEntity,
    LearnerState,
    MasteryEvent,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
    TutoringSession,
)
from apollo.projections.mastery import EVENT_KIND, update_mastery_from_artifact
from apollo.subjects.tests._curriculum_fixtures import (
    minimal_problem_payload,
    problem_database_id,
    seed_concept,
    seed_problems,
    seed_search_space,
)

pytestmark = pytest.mark.integration

_USER_ID = str(uuid.uuid4())


async def _seed_scope(db) -> tuple[int, int]:
    sid = await seed_search_space(db)
    cid = await seed_concept(
        db,
        search_space_id=sid,
        subject_slug=f"subj-{uuid.uuid4().hex[:8]}",
        concept_slug="bernoulli",
    )
    return sid, cid


async def _seed_entity(
    db, *, course_id: int, concept_id: int, canonical_key: str, kind: str = "equation"
) -> int:
    entity = LearnerEntity(
        course_id=course_id,
        concept_id=concept_id,
        canonical_key=canonical_key,
        kind=kind,
        display_name=canonical_key,
    )
    db.add(entity)
    await db.flush()
    return int(entity.id)


async def _seed_problem(db, *, search_space_id: int) -> int:
    """A real ``app.problems`` row under a throwaway concept, decoupled from
    the attempt's own session concept (which some tests deliberately leave
    ``None``) -- only ``GradingRun.problem_id``'s FK cares that it exists."""
    problem_concept_id = await seed_concept(
        db,
        search_space_id=search_space_id,
        subject_slug=f"subj-{uuid.uuid4().hex[:8]}",
        concept_slug=f"pconcept-{uuid.uuid4().hex[:8]}",
    )
    code = f"p-{uuid.uuid4().hex[:8]}"
    await seed_problems(db, concept_id=problem_concept_id, payloads=[minimal_problem_payload(code=code)])
    return await problem_database_id(db, concept_id=problem_concept_id, problem_code=code)


async def _seed_attempt(db, *, search_space_id: int, concept_id: int | None) -> tuple[int, int]:
    """A real ``app.problem_attempts`` row plus a real ``app.problems`` row —
    ``GradingRun.attempt_id``/``problem_id`` are both NOT NULL FKs, so a bare
    literal id (as the pure/mocked artifact_writer tests use) is not enough
    here. Returns ``(attempt_id, problem_id)``."""
    problem_id = await _seed_problem(db, search_space_id=search_space_id)
    sess = TutoringSession(
        user_id=_USER_ID,
        search_space_id=search_space_id,
        concept_id=concept_id,
        # "ended" (not "active"): the partial-unique-index
        # learning_activities__active_tutoring_user_course__uidx allows one ACTIVE
        # session per user, and several tests in this module seed more than
        # one session for the same _USER_ID.
        status=SessionStatus.ended.value,
        phase=SessionPhase.REPORT.value,
        current_problem_id=1,
    )
    db.add(sess)
    await db.flush()
    attempt = ProblemAttempt(
        session_id=sess.id,
        problem_id=problem_id,
        difficulty="intro",
        result="graded",
        user_id=sess.user_id,
        course_id=sess.course_id,
    )
    db.add(attempt)
    await db.flush()
    return int(attempt.id), problem_id


def _artifact_row(
    *,
    attempt_id: int,
    problem_id: int,
    user_id: str,
    search_space_id: int,
    concept_id: int | None,
    composite: float,
    node_ledger: list[dict],
    normalization_confidence: float | None = 0.8,
    role: str = "canonical",
) -> GradingRun:
    return GradingRun(
        attempt_id=attempt_id,
        role=role,
        grader_used="graph",
        grader_version="v1",
        user_id=user_id,
        search_space_id=search_space_id,
        concept_id=concept_id,
        problem_id=problem_id,
        version_details={"grader": "v1"},
        node_ledger=node_ledger,
        edge_ledger=[],
        score_details={"composite": composite},
        composite_score=composite,
        abstained=False,
        abstention_details={"normalization_confidence": normalization_confidence},
        grader_payload={"misconceptions": [], "clarification_trace": []},
        grading_latency_ms=None,
    )


def _ledger(*keys_and_status: tuple[str, str]) -> list[dict]:
    return [
        {
            "canonical_key": key,
            "status": status,
            "method": None,
            "confidence": None,
            "evidence_span": "",
        }
        for key, status in keys_and_status
    ]


async def test_cold_start_mastery_equals_composite(db_session):
    sid, cid = await _seed_scope(db_session)
    entity_id = await _seed_entity(
        db_session, course_id=sid, concept_id=cid, canonical_key="eq.bernoulli"
    )
    attempt_id, problem_id = await _seed_attempt(db_session, search_space_id=sid, concept_id=cid)
    artifact = _artifact_row(
        attempt_id=attempt_id,
        problem_id=problem_id,
        user_id=_USER_ID,
        search_space_id=sid,
        concept_id=cid,
        composite=0.72,
        node_ledger=_ledger(("eq.bernoulli", "credited")),
    )
    db_session.add(artifact)
    await db_session.flush()

    await update_mastery_from_artifact(db_session, artifact_row=artifact)
    await db_session.commit()

    state = (
        await db_session.execute(
            select(LearnerState).where(
                LearnerState.user_id == _USER_ID,
                LearnerState.entity_id == entity_id,
            )
        )
    ).scalar_one()
    assert state.mastery == pytest.approx(0.72)
    assert state.confidence == pytest.approx(0.8)
    assert state.evidence_count == 1

    event = (
        await db_session.execute(
            select(MasteryEvent).where(
                MasteryEvent.attempt_id == attempt_id,
                MasteryEvent.entity_id == entity_id,
            )
        )
    ).scalar_one()
    assert event.event_kind == EVENT_KIND
    assert event.mastery_after == pytest.approx(0.72)
    assert event.score == pytest.approx(0.72)


async def test_second_attempt_moves_mastery_toward_new_composite(db_session, monkeypatch):
    monkeypatch.setenv("APOLLO_MASTERY_EWMA_ALPHA", "0.5")
    sid, cid = await _seed_scope(db_session)
    entity_id = await _seed_entity(
        db_session, course_id=sid, concept_id=cid, canonical_key="eq.bernoulli"
    )

    attempt_id_1, problem_id_1 = await _seed_attempt(
        db_session, search_space_id=sid, concept_id=cid
    )
    first = _artifact_row(
        attempt_id=attempt_id_1,
        problem_id=problem_id_1,
        user_id=_USER_ID,
        search_space_id=sid,
        concept_id=cid,
        composite=1.0,
        node_ledger=_ledger(("eq.bernoulli", "credited")),
    )
    db_session.add(first)
    await db_session.flush()
    await update_mastery_from_artifact(db_session, artifact_row=first)
    await db_session.commit()

    attempt_id_2, problem_id_2 = await _seed_attempt(
        db_session, search_space_id=sid, concept_id=cid
    )
    second = _artifact_row(
        attempt_id=attempt_id_2,
        problem_id=problem_id_2,
        user_id=_USER_ID,
        search_space_id=sid,
        concept_id=cid,
        composite=0.0,
        node_ledger=_ledger(("eq.bernoulli", "credited")),
    )
    db_session.add(second)
    await db_session.flush()
    await update_mastery_from_artifact(db_session, artifact_row=second)
    await db_session.commit()

    state = (
        await db_session.execute(
            select(LearnerState).where(
                LearnerState.user_id == _USER_ID,
                LearnerState.entity_id == entity_id,
            )
        )
    ).scalar_one()
    # alpha=0.5: pass1 mastery=1.0; pass2 = 0.5*0.0 + 0.5*1.0 = 0.5
    assert state.mastery == pytest.approx(0.5)
    assert state.evidence_count == 2

    events = (
        (await db_session.execute(select(MasteryEvent).where(MasteryEvent.entity_id == entity_id)))
        .scalars()
        .all()
    )
    assert len(events) == 2


async def test_retry_of_same_attempt_is_idempotent(db_session):
    sid, cid = await _seed_scope(db_session)
    entity_id = await _seed_entity(
        db_session, course_id=sid, concept_id=cid, canonical_key="eq.bernoulli"
    )
    attempt_id, problem_id = await _seed_attempt(db_session, search_space_id=sid, concept_id=cid)
    artifact = _artifact_row(
        attempt_id=attempt_id,
        problem_id=problem_id,
        user_id=_USER_ID,
        search_space_id=sid,
        concept_id=cid,
        composite=0.9,
        node_ledger=_ledger(("eq.bernoulli", "credited")),
    )
    db_session.add(artifact)
    await db_session.flush()

    await update_mastery_from_artifact(db_session, artifact_row=artifact)
    await db_session.commit()
    # Retry: SAME attempt_id/entity_id — must be a full no-op.
    await update_mastery_from_artifact(db_session, artifact_row=artifact)
    await db_session.commit()

    events = (
        (
            await db_session.execute(
                select(MasteryEvent).where(
                    MasteryEvent.attempt_id == attempt_id, MasteryEvent.entity_id == entity_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1

    state = (
        await db_session.execute(
            select(LearnerState).where(
                LearnerState.user_id == _USER_ID,
                LearnerState.entity_id == entity_id,
            )
        )
    ).scalar_one()
    assert state.evidence_count == 1


async def test_unresolved_ledger_rows_and_unmapped_keys_are_skipped(db_session):
    sid, cid = await _seed_scope(db_session)
    attempt_id, problem_id = await _seed_attempt(db_session, search_space_id=sid, concept_id=cid)
    artifact = _artifact_row(
        attempt_id=attempt_id,
        problem_id=problem_id,
        user_id=_USER_ID,
        search_space_id=sid,
        concept_id=cid,
        composite=0.5,
        node_ledger=_ledger(
            ("stu_node_1", "unresolved"),
            ("eq.does_not_exist", "credited"),
        ),
    )
    db_session.add(artifact)
    await db_session.flush()

    await update_mastery_from_artifact(db_session, artifact_row=artifact)
    await db_session.commit()

    events = (await db_session.execute(select(MasteryEvent))).scalars().all()
    assert events == []


async def test_no_op_when_concept_id_is_none(db_session):
    sid, _cid = await _seed_scope(db_session)
    attempt_id, problem_id = await _seed_attempt(db_session, search_space_id=sid, concept_id=None)
    artifact = _artifact_row(
        attempt_id=attempt_id,
        problem_id=problem_id,
        user_id=_USER_ID,
        search_space_id=sid,
        concept_id=None,
        composite=0.5,
        node_ledger=_ledger(("eq.bernoulli", "credited")),
    )
    db_session.add(artifact)
    await db_session.flush()

    await update_mastery_from_artifact(db_session, artifact_row=artifact)
    await db_session.commit()

    events = (await db_session.execute(select(MasteryEvent))).scalars().all()
    assert events == []


async def test_no_op_when_ledger_has_no_credited_or_misconception_rows(db_session):
    sid, cid = await _seed_scope(db_session)
    attempt_id, problem_id = await _seed_attempt(db_session, search_space_id=sid, concept_id=cid)
    artifact = _artifact_row(
        attempt_id=attempt_id,
        problem_id=problem_id,
        user_id=_USER_ID,
        search_space_id=sid,
        concept_id=cid,
        composite=0.5,
        node_ledger=[],
    )
    db_session.add(artifact)
    await db_session.flush()

    await update_mastery_from_artifact(db_session, artifact_row=artifact)
    await db_session.commit()

    events = (await db_session.execute(select(MasteryEvent))).scalars().all()
    assert events == []


async def test_misconception_key_projects_like_credited(db_session):
    """The subject under test is the ledger row's ``status="misconception"``
    (an open JSON value on ``node_ledger``, unrelated to the entity's SQL-CHECK'd
    ``kind`` column) resolving and projecting like a credited row —
    ``_entity_id_lookups`` only ever keys on ``canonical_key``. DB-13 dropped
    ``kind='misconception'`` from ``learner_entities__kind__check``, so the seeded
    entity here uses a surviving kind ("equation"); the misconception-ness lives
    entirely in the ledger status, not the entity kind."""
    sid, cid = await _seed_scope(db_session)
    entity_id = await _seed_entity(
        db_session, course_id=sid, concept_id=cid, canonical_key="misc.reversal", kind="equation"
    )
    attempt_id, problem_id = await _seed_attempt(db_session, search_space_id=sid, concept_id=cid)
    artifact = _artifact_row(
        attempt_id=attempt_id,
        problem_id=problem_id,
        user_id=_USER_ID,
        search_space_id=sid,
        concept_id=cid,
        composite=0.3,
        node_ledger=_ledger(("misc.reversal", "misconception")),
        normalization_confidence=None,
    )
    db_session.add(artifact)
    await db_session.flush()

    await update_mastery_from_artifact(db_session, artifact_row=artifact)
    await db_session.commit()

    state = (
        await db_session.execute(select(LearnerState).where(LearnerState.entity_id == entity_id))
    ).scalar_one()
    assert state.mastery == pytest.approx(0.3)
    # normalization_confidence absent -> default full confidence (1.0).
    assert state.confidence == pytest.approx(1.0)
