"""Real-PG gate for canonical transcript/topic artifact capture on Done.

Artifact capture is UNCONDITIONAL post flag-reset (the artifact-capture flag
was deleted): every Done click writes exactly one canonical
``internal.grading_runs`` row and attaches the student scorecard. The
transcript grader is patched to a
deterministic coverage dict so no live LLM / network call runs.

These MUST RUN GREEN (not skip) with Docker up — a skip is a FAIL of the gate.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from apollo.conftest import TEST_USER_ID
from apollo.handlers.done import handle_done
from apollo.ontology import KGGraph, build_node
from apollo.persistence.models import (
    GradingRun,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
    TutoringMessage,
    TutoringSession,
)
from apollo.subjects.tests._curriculum_fixtures import (
    load_bernoulli_problem_payloads,
    problem_database_id,
    seed_course,
)

pytestmark = pytest.mark.integration

_INTRO = [p for p in load_bernoulli_problem_payloads() if p["difficulty"] == "intro"]

# Deterministic transcript-grader coverage (2 covered, 1 missing) so the
# canonical node ledger is genuinely non-empty and no live LLM runs.
_COVERAGE = {
    "per_step": {"a": "covered", "b": "covered", "c": "missing"},
    "procedure_scores": {},
    "confidences": {"a": 0.9, "b": 0.8, "c": 0.2},
    "negotiation_counts": {"dual": 0, "disputed": 0, "paraphrased": 0, "skipped": 0},
}


def _student_graph(attempt_id: int) -> KGGraph:
    node = build_node(
        node_type="equation",
        node_id="stu_continuity",
        attempt_id=attempt_id,
        source="parser",
        content={"symbolic": "rho*A1*v1 - rho*A2*v2", "label": "continuity", "variables": []},
        parser_confidence=0.95,
    )
    return KGGraph(nodes=[node], edges=[])


async def _seed_session(db, *, current_code: str, sid: int, cid: int):
    current_problem_id = await problem_database_id(db, concept_id=cid, problem_code=current_code)
    sess = TutoringSession(
        user_id=TEST_USER_ID,
        search_space_id=sid,
        concept_id=cid,
        status=SessionStatus.active.value,
        phase=SessionPhase.SOLVING.value,
        current_problem_id=current_problem_id,
    )
    db.add(sess)
    await db.flush()
    attempt = ProblemAttempt(
        session_id=sess.id,
        problem_id=current_problem_id,
        difficulty="intro",
        user_id=sess.user_id,
        course_id=sess.course_id,
    )
    db.add(attempt)
    await db.flush()
    db.add(
        TutoringMessage(
            session_id=sess.id,
            course_id=sess.course_id,
            attempt_id=attempt.id,
            role="student",
            content="continuity says rho A1 v1 equals rho A2 v2",
            turn_index=0,
        )
    )
    await db.flush()
    return sess, attempt


def _neo_stubs(attempt_id: int):
    return [
        patch(
            "apollo.handlers.done.KGStore.read_graph",
            new=AsyncMock(return_value=_student_graph(attempt_id)),
        ),
        patch("apollo.handlers.done.KGStore.freeze", new=AsyncMock()),
        patch("apollo.handlers.done.KGStore.stamp_graded_at", new=AsyncMock()),
    ]


def _grading_stubs(extra=None):
    """Patch the transcript grader to a deterministic coverage dict (no live
    LLM). ``extra`` appends further patches (e.g. a fixed ``compute_rubric``)."""
    stubs = [
        patch(
            "apollo.handlers.done.compute_transcript_coverage_with_spans",
            new=AsyncMock(return_value=(dict(_COVERAGE), {})),
        ),
    ]
    return stubs + list(extra or [])


def _start(patches):
    for p in patches:
        p.start()


def _stop(patches):
    for p in reversed(patches):
        p.stop()


async def _artifact_rows(db, attempt_id: int):
    return (
        (await db.execute(select(GradingRun).where(GradingRun.attempt_id == attempt_id)))
        .scalars()
        .all()
    )


async def test_done_writes_one_canonical_artifact_and_scorecard(db_session):
    """Every Done writes exactly one canonical row and attaches the scorecard —
    artifact capture is unconditional now."""
    sid, cid, codes = await seed_course(
        db_session,
        subject_slug="fluid_mechanics",
        concept_slug="bernoulli_principle",
        problems=_INTRO,
    )
    sess, attempt = await _seed_session(db_session, current_code=codes[0], sid=sid, cid=cid)

    patches = _neo_stubs(attempt.id) + _grading_stubs()
    _start(patches)
    try:
        out = await handle_done(db=db_session, neo=object(), session_id=sess.id)
    finally:
        _stop(patches)

    rows = await _artifact_rows(db_session, attempt.id)
    assert len(rows) == 1
    assert rows[0].role == "canonical"
    assert rows[0].grader_used == "llm_fallback"

    # Scorecard is projected from the persisted canonical payload.
    assert "scorecard" in out
    assert set(out["scorecard"]) == {
        "score_0_100",
        "band",
        "taught_well",
        "missing_or_unclear",
        "watch_out",
        # Lane B3a/D1 — empty-bank ("not checked") vs found-none disambiguation.
        "watch_out_status",
        "watch_out_note",
        "clarifications",
    }
    # Provenance ships on every Done and reports the transcript lane.
    assert out["grading_provenance"]["grader_used"] == "llm_transcript"
    assert out["grading_provenance"]["evidence_source"] == "transcript"


async def test_canonical_composite_matches_axis_rubric_and_band(db_session):
    """T1 regression (defect: canonical artifact built from an EMPTY coverage
    dict silently graded composite=0.0). Drives the real Done route and asserts
    the persisted canonical artifact's composite reflects the axis rubric
    overall (what ``build_llm_artifact`` reads) with a real, non-empty node
    ledger, and the scorecard band is consistent with it."""
    from apollo.projections.scorecard import _band_for, load_bands

    sid, cid, codes = await seed_course(
        db_session,
        subject_slug="fluid_mechanics",
        concept_slug="bernoulli_principle",
        problems=_INTRO,
    )
    sess, attempt = await _seed_session(db_session, current_code=codes[0], sid=sid, cid=cid)

    fixed_rubric = {
        "overall": {"score": 70, "letter": "B-"},
        "procedure": {"score": 70, "letter": "B-", "present": True},
        "justification": {"score": 70, "letter": "B-", "present": True},
        "simplification": {"score": 70, "letter": "B-", "present": True},
    }
    patches = _neo_stubs(attempt.id) + _grading_stubs(
        extra=[patch("apollo.handlers.done.compute_rubric", return_value=dict(fixed_rubric))]
    )
    _start(patches)
    try:
        out = await handle_done(db=db_session, neo=object(), session_id=sess.id)
    finally:
        _stop(patches)

    rows = await _artifact_rows(db_session, attempt.id)
    canonical = next(r for r in rows if r.role == "canonical")
    assert canonical.grader_used == "llm_fallback"

    # Composite is the axis rubric overall / 100 — build_llm_artifact reads the
    # raw `rubric`, not the served topic overall.
    expected_composite = round(70 / 100.0, 6)
    assert canonical.composite_score == pytest.approx(expected_composite)
    assert canonical.composite_score > 0.0
    # The node ledger carries real entries (not the empty list a covered/missing
    # key mismatch silently produced).
    assert canonical.node_ledger != []

    expected_band = _band_for(expected_composite, load_bands())
    assert out["scorecard"]["band"] == expected_band
    assert canonical.composite_score * 100 == pytest.approx(out["scorecard"]["score_0_100"], abs=1)
