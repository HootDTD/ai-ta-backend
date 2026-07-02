"""Campaign-plan Task B3 — real-PG behavioral gate for
``apollo.projections.classroom.mastery_heatmap`` /
``apollo.projections.classroom.struggle_signals``.

Seeds 2 students x 2 concepts of ``apollo_learner_state`` rows plus 3
canonical ``apollo_grading_artifacts`` rows (one abstained, one LLM-fallback,
one with a misconception asserted twice across two attempts) directly via the
ORM -- these tests exercise the AGGREGATION SQL, not the writers that produce
these rows in production (``update_mastery_from_artifact`` / `write_artifacts`
already have their own dedicated DB test suites).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from apollo.persistence.models import (
    ApolloSession,
    GradingArtifact,
    KGEntity,
    LearnerState,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
)
from apollo.projections.classroom import mastery_heatmap, struggle_signals
from apollo.subjects.tests._curriculum_fixtures import seed_concept, seed_search_space

pytestmark = pytest.mark.integration


async def _seed_two_concepts(db) -> tuple[int, int, int]:
    sid = await seed_search_space(db)
    cid_1 = await seed_concept(
        db, search_space_id=sid, subject_slug=f"subj-{uuid.uuid4().hex[:8]}", concept_slug="c1"
    )
    cid_2 = await seed_concept(
        db, search_space_id=sid, subject_slug=f"subj-{uuid.uuid4().hex[:8]}", concept_slug="c2"
    )
    return sid, cid_1, cid_2


async def _seed_entity(db, *, concept_id: int, canonical_key: str, kind: str = "equation") -> int:
    entity = KGEntity(
        concept_id=concept_id, canonical_key=canonical_key, kind=kind, display_name=canonical_key,
    )
    db.add(entity)
    await db.flush()
    return int(entity.id)


async def _seed_state(
    db, *, user_id: str, search_space_id: int, entity_id: int, mastery: float, confidence: float
) -> None:
    db.add(
        LearnerState(
            user_id=user_id,
            search_space_id=search_space_id,
            entity_id=entity_id,
            belief=[1.0 - mastery, 0.0, mastery],
            mastery=mastery,
            confidence=confidence,
            evidence_count=1,
        )
    )
    await db.flush()


async def _seed_attempt(db, *, search_space_id: int, concept_id: int) -> int:
    sess = ApolloSession(
        user_id=str(uuid.uuid4()),
        search_space_id=search_space_id,
        concept_id=concept_id,
        status=SessionStatus.ended.value,
        phase=SessionPhase.REPORT.value,
        current_problem_id="p1",
    )
    db.add(sess)
    await db.flush()
    attempt = ProblemAttempt(session_id=sess.id, problem_id="p1", difficulty="intro", result="graded")
    db.add(attempt)
    await db.flush()
    return int(attempt.id)


def _artifact(
    *,
    attempt_id: int,
    search_space_id: int,
    concept_id: int,
    grader_used: str,
    node_ledger: list[dict] | None = None,
    misconceptions: list[dict] | None = None,
    abstained: bool | None = None,
    created_at: datetime | None = None,
) -> GradingArtifact:
    return GradingArtifact(
        attempt_id=attempt_id,
        role="canonical",
        grader_used=grader_used,
        user_id=str(uuid.uuid4()),
        search_space_id=search_space_id,
        concept_id=concept_id,
        problem_id="p1",
        versions={"grader": "v1"},
        node_ledger=node_ledger or [],
        edge_ledger=[],
        misconceptions=misconceptions or [],
        clarification_trace=[],
        scores={"composite": 0.5},
        abstention={"abstained": abstained} if abstained is not None else None,
        grading_latency_ms=None,
        created_at=created_at or datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# mastery_heatmap
# ---------------------------------------------------------------------------


async def test_heatmap_returns_one_row_per_user_and_concept(db_session):
    sid, cid_1, cid_2 = await _seed_two_concepts(db_session)
    e1 = await _seed_entity(db_session, concept_id=cid_1, canonical_key="eq.a")
    e2 = await _seed_entity(db_session, concept_id=cid_2, canonical_key="eq.b")
    u1, u2 = str(uuid.uuid4()), str(uuid.uuid4())

    await _seed_state(db_session, user_id=u1, search_space_id=sid, entity_id=e1, mastery=0.8, confidence=0.9)
    await _seed_state(db_session, user_id=u1, search_space_id=sid, entity_id=e2, mastery=0.4, confidence=0.5)
    await _seed_state(db_session, user_id=u2, search_space_id=sid, entity_id=e1, mastery=0.2, confidence=0.3)
    await db_session.commit()

    rows = await mastery_heatmap(db_session, search_space_id=sid)
    by_key = {(r["user_id"], r["concept_id"]): r for r in rows}

    assert len(rows) == 3
    assert by_key[(u1, cid_1)]["mastery"] == pytest.approx(0.8)
    assert by_key[(u1, cid_1)]["confidence"] == pytest.approx(0.9)
    assert by_key[(u1, cid_2)]["mastery"] == pytest.approx(0.4)
    assert by_key[(u2, cid_1)]["mastery"] == pytest.approx(0.2)


async def test_heatmap_averages_multiple_entities_under_one_concept(db_session):
    sid, cid_1, _cid_2 = await _seed_two_concepts(db_session)
    e1 = await _seed_entity(db_session, concept_id=cid_1, canonical_key="eq.a")
    e2 = await _seed_entity(db_session, concept_id=cid_1, canonical_key="eq.b")
    u1 = str(uuid.uuid4())

    await _seed_state(db_session, user_id=u1, search_space_id=sid, entity_id=e1, mastery=1.0, confidence=1.0)
    await _seed_state(db_session, user_id=u1, search_space_id=sid, entity_id=e2, mastery=0.0, confidence=0.0)
    await db_session.commit()

    rows = await mastery_heatmap(db_session, search_space_id=sid)
    assert len(rows) == 1
    assert rows[0]["mastery"] == pytest.approx(0.5)
    assert rows[0]["confidence"] == pytest.approx(0.5)


async def test_heatmap_scopes_to_search_space(db_session):
    sid_a, cid_a, _ = await _seed_two_concepts(db_session)
    sid_b, cid_b, _ = await _seed_two_concepts(db_session)
    e_a = await _seed_entity(db_session, concept_id=cid_a, canonical_key="eq.a")
    e_b = await _seed_entity(db_session, concept_id=cid_b, canonical_key="eq.b")
    u1 = str(uuid.uuid4())

    await _seed_state(db_session, user_id=u1, search_space_id=sid_a, entity_id=e_a, mastery=0.7, confidence=0.7)
    await _seed_state(db_session, user_id=u1, search_space_id=sid_b, entity_id=e_b, mastery=0.1, confidence=0.1)
    await db_session.commit()

    rows = await mastery_heatmap(db_session, search_space_id=sid_a)
    assert len(rows) == 1
    assert rows[0]["concept_id"] == cid_a


async def test_heatmap_empty_when_no_state_rows(db_session):
    sid, _cid_1, _cid_2 = await _seed_two_concepts(db_session)
    assert await mastery_heatmap(db_session, search_space_id=sid) == []


# ---------------------------------------------------------------------------
# struggle_signals
# ---------------------------------------------------------------------------


async def test_struggle_signals_counts_and_top_lists(db_session):
    sid, cid_1, _cid_2 = await _seed_two_concepts(db_session)

    attempt_1 = await _seed_attempt(db_session, search_space_id=sid, concept_id=cid_1)
    abstained_artifact = _artifact(
        attempt_id=attempt_1,
        search_space_id=sid,
        concept_id=cid_1,
        grader_used="llm_fallback",
        abstained=True,
        node_ledger=[
            {"canonical_key": "eq.a", "status": "credited"},
            {"canonical_key": "eq.b", "status": "misconception"},
        ],
        misconceptions=[{"canonical_key": "misc.x", "evidence_span": "e1", "confidence": 0.9}],
    )
    db_session.add(abstained_artifact)
    await db_session.flush()

    attempt_2 = await _seed_attempt(db_session, search_space_id=sid, concept_id=cid_1)
    fallback_artifact = _artifact(
        attempt_id=attempt_2,
        search_space_id=sid,
        concept_id=cid_1,
        grader_used="llm_fallback",
        abstained=False,
        node_ledger=[{"canonical_key": "eq.b", "status": "misconception"}],
        misconceptions=[{"canonical_key": "misc.x", "evidence_span": "e2", "confidence": 0.8}],
    )
    db_session.add(fallback_artifact)
    await db_session.flush()

    attempt_3 = await _seed_attempt(db_session, search_space_id=sid, concept_id=cid_1)
    graph_artifact = _artifact(
        attempt_id=attempt_3,
        search_space_id=sid,
        concept_id=cid_1,
        grader_used="graph",
        abstained=False,
        node_ledger=[{"canonical_key": "eq.a", "status": "credited"}],
    )
    db_session.add(graph_artifact)
    await db_session.commit()

    result = await struggle_signals(db_session, search_space_id=sid, window_days=14)

    assert result["abstention_count"] == 1
    assert result["fallback_count"] == 2

    coverage_by_key = {row["key"]: row for row in result["lowest_coverage_nodes"]}
    # eq.b is credited 0/2 times -> lowest coverage; eq.a is credited 2/2.
    assert coverage_by_key["eq.b"]["mean_coverage"] == pytest.approx(0.0)
    assert coverage_by_key["eq.b"]["n"] == 2
    assert coverage_by_key["eq.a"]["mean_coverage"] == pytest.approx(1.0)
    assert coverage_by_key["eq.a"]["n"] == 2
    # eq.b (0.0 coverage) sorts before eq.a (1.0 coverage).
    assert [row["key"] for row in result["lowest_coverage_nodes"]] == ["eq.b", "eq.a"]

    assert result["top_misconceptions"] == [{"key": "misc.x", "count": 2}]


async def test_struggle_signals_unresolved_student_ids_excluded_from_coverage(db_session):
    sid, cid_1, _cid_2 = await _seed_two_concepts(db_session)
    attempt = await _seed_attempt(db_session, search_space_id=sid, concept_id=cid_1)
    artifact = _artifact(
        attempt_id=attempt,
        search_space_id=sid,
        concept_id=cid_1,
        grader_used="graph",
        abstained=False,
        node_ledger=[{"canonical_key": "student-node-9", "status": "unresolved"}],
    )
    db_session.add(artifact)
    await db_session.commit()

    result = await struggle_signals(db_session, search_space_id=sid)
    assert result["lowest_coverage_nodes"] == []


async def test_struggle_signals_respects_window(db_session):
    sid, cid_1, _cid_2 = await _seed_two_concepts(db_session)
    attempt = await _seed_attempt(db_session, search_space_id=sid, concept_id=cid_1)
    stale = _artifact(
        attempt_id=attempt,
        search_space_id=sid,
        concept_id=cid_1,
        grader_used="llm_fallback",
        abstained=True,
        created_at=datetime.now(UTC) - timedelta(days=30),
    )
    db_session.add(stale)
    await db_session.commit()

    result = await struggle_signals(db_session, search_space_id=sid, window_days=14)
    assert result["abstention_count"] == 0
    assert result["fallback_count"] == 0


async def test_struggle_signals_other_course_invisible(db_session):
    sid_a, cid_a, _ = await _seed_two_concepts(db_session)
    sid_b, cid_b, _ = await _seed_two_concepts(db_session)
    attempt_a = await _seed_attempt(db_session, search_space_id=sid_a, concept_id=cid_a)
    attempt_b = await _seed_attempt(db_session, search_space_id=sid_b, concept_id=cid_b)
    db_session.add(
        _artifact(
            attempt_id=attempt_a, search_space_id=sid_a, concept_id=cid_a,
            grader_used="llm_fallback", abstained=True,
        )
    )
    db_session.add(
        _artifact(
            attempt_id=attempt_b, search_space_id=sid_b, concept_id=cid_b,
            grader_used="llm_fallback", abstained=True,
        )
    )
    await db_session.commit()

    result_a = await struggle_signals(db_session, search_space_id=sid_a)
    assert result_a["abstention_count"] == 1


async def test_struggle_signals_empty_course(db_session):
    sid, _cid_1, _cid_2 = await _seed_two_concepts(db_session)
    result = await struggle_signals(db_session, search_space_id=sid)
    assert result == {
        "abstention_count": 0,
        "fallback_count": 0,
        "lowest_coverage_nodes": [],
        "top_misconceptions": [],
    }
