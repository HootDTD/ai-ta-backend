"""Campaign-plan Task B3 — real-PG behavioral gate for
``apollo.projections.classroom.mastery_heatmap`` /
``apollo.projections.classroom.struggle_signals``.

Seeds 2 students x 2 concepts of ``apollo_learner_state`` rows plus
``apollo_grading_artifacts`` rows directly via the ORM -- these tests exercise
the AGGREGATION SQL, not the writers that produce these rows in production
(``update_mastery_from_artifact`` / ``write_artifacts`` already have their own
dedicated DB test suites).

Row shapes below mirror what ``apollo.handlers.artifact_writer.write_artifacts``
+ ``apollo.grading.artifact_build`` actually produce (review fix, Task B3):
``build_llm_artifact`` hardcodes ``abstention.abstained = None`` -- abstention
is a GRAPH-grader-only concept -- and an abstained shadow grade always falls
back to LLM for the SERVED (``role='canonical'``) row, so ``abstained=true``
only ever lands on a ``grader_used='graph'`` row, which is ``role='pair'``
whenever the shadow abstained. A "fallback" (``role='canonical'``
``grader_used='llm_fallback'``) is only a REAL fallback-from-graph when a
sibling ``role='pair'`` ``grader_used='graph'`` row exists for the same
attempt -- an attempt graded with the shadow chain off entirely also serves
``llm_fallback`` but has no paired graph row at all, and must not count.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from apollo.persistence.models import (
    GradingArtifact,
    LearnerEntity,
    LearnerState,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
    TutoringSession,
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
    sess = TutoringSession(
        user_id=str(uuid.uuid4()),
        search_space_id=search_space_id,
        concept_id=concept_id,
        status=SessionStatus.ended.value,
        phase=SessionPhase.REPORT.value,
        current_problem_id=1,
    )
    db.add(sess)
    await db.flush()
    attempt = ProblemAttempt(
        session_id=sess.id,
        problem_id=1,
        difficulty="intro",
        result="graded",
        user_id=sess.user_id,
        course_id=sess.course_id,
    )
    db.add(attempt)
    await db.flush()
    return int(attempt.id)


def _artifact(
    *,
    attempt_id: int,
    search_space_id: int,
    concept_id: int,
    grader_used: str,
    role: str = "canonical",
    node_ledger: list[dict] | None = None,
    misconceptions: list[dict] | None = None,
    abstained: bool | None = None,
    created_at: datetime | None = None,
) -> GradingArtifact:
    return GradingArtifact(
        attempt_id=attempt_id,
        role=role,
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
    e1 = await _seed_entity(db_session, course_id=sid, concept_id=cid_1, canonical_key="eq.a")
    e2 = await _seed_entity(db_session, course_id=sid, concept_id=cid_2, canonical_key="eq.b")
    u1, u2 = str(uuid.uuid4()), str(uuid.uuid4())

    await _seed_state(
        db_session, user_id=u1, search_space_id=sid, entity_id=e1, mastery=0.8, confidence=0.9
    )
    await _seed_state(
        db_session, user_id=u1, search_space_id=sid, entity_id=e2, mastery=0.4, confidence=0.5
    )
    await _seed_state(
        db_session, user_id=u2, search_space_id=sid, entity_id=e1, mastery=0.2, confidence=0.3
    )
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
    e1 = await _seed_entity(db_session, course_id=sid, concept_id=cid_1, canonical_key="eq.a")
    e2 = await _seed_entity(db_session, course_id=sid, concept_id=cid_1, canonical_key="eq.b")
    u1 = str(uuid.uuid4())

    await _seed_state(
        db_session, user_id=u1, search_space_id=sid, entity_id=e1, mastery=1.0, confidence=1.0
    )
    await _seed_state(
        db_session, user_id=u1, search_space_id=sid, entity_id=e2, mastery=0.0, confidence=0.0
    )
    await db_session.commit()

    rows = await mastery_heatmap(db_session, search_space_id=sid)
    assert len(rows) == 1
    assert rows[0]["mastery"] == pytest.approx(0.5)
    assert rows[0]["confidence"] == pytest.approx(0.5)


async def test_heatmap_scopes_to_search_space(db_session):
    sid_a, cid_a, _ = await _seed_two_concepts(db_session)
    sid_b, cid_b, _ = await _seed_two_concepts(db_session)
    e_a = await _seed_entity(db_session, course_id=sid_a, concept_id=cid_a, canonical_key="eq.a")
    e_b = await _seed_entity(db_session, course_id=sid_b, concept_id=cid_b, canonical_key="eq.b")
    u1 = str(uuid.uuid4())

    await _seed_state(
        db_session, user_id=u1, search_space_id=sid_a, entity_id=e_a, mastery=0.7, confidence=0.7
    )
    await _seed_state(
        db_session, user_id=u1, search_space_id=sid_b, entity_id=e_b, mastery=0.1, confidence=0.1
    )
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


def _paired_abstained(
    db_session, *, attempt_id, search_space_id, concept_id, created_at=None, **canonical_kwargs
):
    """Realistic ``write_artifacts`` shape for an abstained shadow: the served
    (``role='canonical'``) row is the LLM fallback (``abstained=None``,
    ``build_llm_artifact`` never sets it) and the abstention lives on the
    ``role='pair'`` graph row -- the only shape ``build_graph_artifact`` +
    ``write_artifacts`` can ever actually produce for an abstained attempt."""
    db_session.add(
        _artifact(
            attempt_id=attempt_id,
            search_space_id=search_space_id,
            concept_id=concept_id,
            role="canonical",
            grader_used="llm_fallback",
            abstained=None,
            created_at=created_at,
            **canonical_kwargs,
        )
    )
    db_session.add(
        _artifact(
            attempt_id=attempt_id,
            search_space_id=search_space_id,
            concept_id=concept_id,
            role="pair",
            grader_used="graph",
            abstained=True,
            created_at=created_at,
        )
    )


def _paired_fallback(
    db_session, *, attempt_id, search_space_id, concept_id, created_at=None, **canonical_kwargs
):
    """Realistic ``write_artifacts`` shape for a REAL (non-abstained)
    fallback-from-graph: a shadow ran (paired ``role='pair'`` graph row,
    ``abstained=False``) but LIVE promotion kept ``llm_fallback`` as the
    served ``role='canonical'`` grade."""
    db_session.add(
        _artifact(
            attempt_id=attempt_id,
            search_space_id=search_space_id,
            concept_id=concept_id,
            role="canonical",
            grader_used="llm_fallback",
            abstained=None,
            created_at=created_at,
            **canonical_kwargs,
        )
    )
    db_session.add(
        _artifact(
            attempt_id=attempt_id,
            search_space_id=search_space_id,
            concept_id=concept_id,
            role="pair",
            grader_used="graph",
            abstained=False,
            created_at=created_at,
        )
    )


async def test_struggle_signals_counts_and_top_lists(db_session):
    sid, cid_1, _cid_2 = await _seed_two_concepts(db_session)

    attempt_1 = await _seed_attempt(db_session, search_space_id=sid, concept_id=cid_1)
    _paired_abstained(
        db_session,
        attempt_id=attempt_1,
        search_space_id=sid,
        concept_id=cid_1,
        node_ledger=[
            {"canonical_key": "eq.a", "status": "credited"},
            {"canonical_key": "eq.b", "status": "misconception"},
        ],
        misconceptions=[{"canonical_key": "misc.x", "evidence_span": "e1", "confidence": 0.9}],
    )
    await db_session.flush()

    attempt_2 = await _seed_attempt(db_session, search_space_id=sid, concept_id=cid_1)
    _paired_fallback(
        db_session,
        attempt_id=attempt_2,
        search_space_id=sid,
        concept_id=cid_1,
        node_ledger=[{"canonical_key": "eq.b", "status": "misconception"}],
        misconceptions=[{"canonical_key": "misc.x", "evidence_span": "e2", "confidence": 0.8}],
    )
    await db_session.flush()

    attempt_3 = await _seed_attempt(db_session, search_space_id=sid, concept_id=cid_1)
    graph_artifact = _artifact(
        attempt_id=attempt_3,
        search_space_id=sid,
        concept_id=cid_1,
        role="canonical",
        grader_used="graph",
        abstained=False,
        node_ledger=[{"canonical_key": "eq.a", "status": "credited"}],
    )
    db_session.add(graph_artifact)
    await db_session.commit()

    result = await struggle_signals(db_session, search_space_id=sid, window_days=14)

    # attempt_1's pair row is the only grader_used='graph' row with
    # abstained=true; attempt_1 + attempt_2 are both a canonical
    # llm_fallback row backed by a paired graph row (a REAL fallback).
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


async def test_struggle_signals_counts_abstention_from_paired_graph_row(db_session):
    """Review-fix regression gate: the OLD query counted
    ``role='canonical' AND abstention->>'abstained'='true'``, which is
    structurally dead (``build_llm_artifact`` -- the only thing that can be
    ``role='canonical'`` when a shadow abstained -- hardcodes
    ``abstained=None``). The real abstention signal lives on the
    ``role='pair'`` ``grader_used='graph'`` row; this seeds exactly that
    shape and proves it is now counted."""
    sid, cid_1, _cid_2 = await _seed_two_concepts(db_session)
    attempt = await _seed_attempt(db_session, search_space_id=sid, concept_id=cid_1)
    _paired_abstained(db_session, attempt_id=attempt, search_space_id=sid, concept_id=cid_1)
    await db_session.commit()

    result = await struggle_signals(db_session, search_space_id=sid)
    assert result["abstention_count"] == 1
    assert result["fallback_count"] == 1


async def test_struggle_signals_fallback_excludes_shadow_off_llm_only(db_session):
    """An attempt graded with the shadow chain off entirely also serves
    ``llm_fallback`` on its (only) ``role='canonical'`` row, but there is no
    paired graph row at all -- that is not a "fallback", it is the only
    grader that ever ran. Must not inflate ``fallback_count``."""
    sid, cid_1, _cid_2 = await _seed_two_concepts(db_session)
    attempt = await _seed_attempt(db_session, search_space_id=sid, concept_id=cid_1)
    db_session.add(
        _artifact(
            attempt_id=attempt,
            search_space_id=sid,
            concept_id=cid_1,
            role="canonical",
            grader_used="llm_fallback",
            abstained=None,
        )
    )
    await db_session.commit()

    result = await struggle_signals(db_session, search_space_id=sid)
    assert result["abstention_count"] == 0
    assert result["fallback_count"] == 0


async def test_struggle_signals_unresolved_student_ids_excluded_from_coverage(db_session):
    sid, cid_1, _cid_2 = await _seed_two_concepts(db_session)
    attempt = await _seed_attempt(db_session, search_space_id=sid, concept_id=cid_1)
    artifact = _artifact(
        attempt_id=attempt,
        search_space_id=sid,
        concept_id=cid_1,
        grader_used="graph",
        abstained=False,
        # A real `_unresolved_ledger_entry` (UNRESOLVED student utterance)
        # row: keyed on a student-side id, evidence_span is a (possibly
        # empty) STRING -- never `None` -- distinguishing it from a
        # `_missing_ledger_entry` MISSING_NODE row.
        node_ledger=[
            {"canonical_key": "student-node-9", "status": "unresolved", "evidence_span": ""}
        ],
    )
    db_session.add(artifact)
    await db_session.commit()

    result = await struggle_signals(db_session, search_space_id=sid)
    assert result["lowest_coverage_nodes"] == []


async def test_struggle_signals_missing_node_rows_surface_as_zero_coverage(db_session):
    """Review-fix regression gate: a `_missing_ledger_entry` row (a
    reference node the student never mentioned at all) is keyed on a REAL
    canonical_key with `evidence_span=None` (never attempted, distinct from
    a real failed resolution). It must surface as a 0.0-coverage worst
    offender, not be silently dropped like an opaque student-id row."""
    sid, cid_1, _cid_2 = await _seed_two_concepts(db_session)
    attempt = await _seed_attempt(db_session, search_space_id=sid, concept_id=cid_1)
    artifact = _artifact(
        attempt_id=attempt,
        search_space_id=sid,
        concept_id=cid_1,
        grader_used="graph",
        abstained=False,
        node_ledger=[
            {"canonical_key": "eq.a", "status": "credited"},
            {
                "canonical_key": "eq.never-taught",
                "status": "unresolved",
                "evidence_span": None,
                "confidence": None,
            },
        ],
    )
    db_session.add(artifact)
    await db_session.commit()

    result = await struggle_signals(db_session, search_space_id=sid)
    coverage_by_key = {row["key"]: row for row in result["lowest_coverage_nodes"]}
    assert coverage_by_key["eq.never-taught"]["mean_coverage"] == pytest.approx(0.0)
    assert coverage_by_key["eq.never-taught"]["n"] == 1
    assert coverage_by_key["eq.a"]["mean_coverage"] == pytest.approx(1.0)
    assert [row["key"] for row in result["lowest_coverage_nodes"]][0] == "eq.never-taught"


async def test_struggle_signals_respects_window(db_session):
    sid, cid_1, _cid_2 = await _seed_two_concepts(db_session)
    attempt = await _seed_attempt(db_session, search_space_id=sid, concept_id=cid_1)
    stale_created_at = datetime.now(UTC) - timedelta(days=30)
    _paired_abstained(
        db_session,
        attempt_id=attempt,
        search_space_id=sid,
        concept_id=cid_1,
        created_at=stale_created_at,
    )
    await db_session.commit()

    result = await struggle_signals(db_session, search_space_id=sid, window_days=14)
    assert result["abstention_count"] == 0
    assert result["fallback_count"] == 0


async def test_struggle_signals_other_course_invisible(db_session):
    sid_a, cid_a, _ = await _seed_two_concepts(db_session)
    sid_b, cid_b, _ = await _seed_two_concepts(db_session)
    attempt_a = await _seed_attempt(db_session, search_space_id=sid_a, concept_id=cid_a)
    attempt_b = await _seed_attempt(db_session, search_space_id=sid_b, concept_id=cid_b)
    _paired_abstained(db_session, attempt_id=attempt_a, search_space_id=sid_a, concept_id=cid_a)
    _paired_abstained(db_session, attempt_id=attempt_b, search_space_id=sid_b, concept_id=cid_b)
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
