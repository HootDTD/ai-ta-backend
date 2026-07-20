"""Real-PG gate for canonical transcript/topic artifact capture on Done.

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
    TutoringSession,
    GradingArtifact,
    TutoringMessage,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
)
from apollo.subjects.tests._curriculum_fixtures import (
    load_bernoulli_problem_payloads,
    seed_course,
)

pytestmark = pytest.mark.integration

_INTRO = [p for p in load_bernoulli_problem_payloads() if p["difficulty"] == "intro"]


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
    sess = TutoringSession(
        user_id=TEST_USER_ID,
        search_space_id=sid,
        concept_id=cid,
        status=SessionStatus.active.value,
        phase=SessionPhase.SOLVING.value,
        current_problem_id=current_code,
    )
    db.add(sess)
    await db.flush()
    attempt = ProblemAttempt(session_id=sess.id, problem_id=current_code, difficulty="intro")
    db.add(attempt)
    await db.flush()
    db.add(
        TutoringMessage(
            session_id=sess.id,
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


def _start(patches):
    for p in patches:
        p.start()


def _stop(patches):
    for p in reversed(patches):
        p.stop()


async def _run_done(db, sess_id, monkeypatch, *, neo_patches, artifact_on: bool):
    if artifact_on:
        monkeypatch.setenv("APOLLO_GRADING_ARTIFACT_ENABLED", "true")
    _start(neo_patches)
    try:
        return await handle_done(db=db, neo=object(), session_id=sess_id)
    finally:
        _stop(neo_patches)


async def _artifact_rows(db, attempt_id: int):
    return (
        (await db.execute(select(GradingArtifact).where(GradingArtifact.attempt_id == attempt_id)))
        .scalars()
        .all()
    )


async def test_both_flags_off_writes_zero_rows_response_unchanged(db_session, monkeypatch):
    """With artifact flags off, only unconditional response fields are added."""
    sid, cid, codes = await seed_course(
        db_session,
        subject_slug="fluid_mechanics",
        concept_slug="bernoulli_principle",
        problems=_INTRO,
    )
    sess, attempt = await _seed_session(db_session, current_code=codes[0], sid=sid, cid=cid)

    out = await _run_done(
        db_session,
        sess.id,
        monkeypatch,
        neo_patches=_neo_stubs(attempt.id),
        artifact_on=False,
    )

    assert set(out) == {
        "rubric",
        "diagnostic_narrative",
        "coverage",
        "progress",
        "xp_earned",
        "xp_before",
        "xp_after",
        "level_before",
        "level_after",
        "level_up",
        # Feedback transcript: unconditional presentation data; [] on
        # retrieval soft-fail and independent of artifact/shadow flags.
        "transcript",
        # Part 1 transcript grader: provenance ships in BOTH flag states
        # (spec §3.7.3) — present even with every apollo flag off.
        "grading_provenance",
    }
    rows = await _artifact_rows(db_session, attempt.id)
    assert rows == []


async def test_artifact_on_shadow_off_writes_one_llm_canonical_row(db_session, monkeypatch):
    sid, cid, codes = await seed_course(
        db_session,
        subject_slug="fluid_mechanics",
        concept_slug="bernoulli_principle",
        problems=_INTRO,
    )
    sess, attempt = await _seed_session(db_session, current_code=codes[0], sid=sid, cid=cid)

    out = await _run_done(
        db_session,
        sess.id,
        monkeypatch,
        neo_patches=_neo_stubs(attempt.id),
        artifact_on=True,
    )

    rows = await _artifact_rows(db_session, attempt.id)
    assert len(rows) == 1
    assert rows[0].role == "canonical"
    assert rows[0].grader_used == "llm_fallback"

    # Task B1 — artifact capture on ⇒ handle_done renders + attaches the
    # student scorecard from the persisted canonical row's payload.
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


def _student_graph_with_condition_credit(attempt_id: int) -> KGGraph:
    """Same continuity-equation node as ``_student_graph``, PLUS a condition
    node so the "justification" rubric axis (bernoulli_principle problem_01's
    ``cond.incompressibility``) has real student evidence to match against —
    the bare-equation-only ``_student_graph`` fixture legitimately grades 0
    across every rubric axis (no procedure/condition/simplification content
    at all), which would make a composite-matches-rubric regression test
    vacuous."""
    eq_node = build_node(
        node_type="equation",
        node_id="stu_continuity",
        attempt_id=attempt_id,
        source="parser",
        content={"symbolic": "rho*A1*v1 - rho*A2*v2", "label": "continuity", "variables": []},
        parser_confidence=0.95,
    )
    cond_node = build_node(
        node_type="condition",
        node_id="stu_incompressible",
        attempt_id=attempt_id,
        source="parser",
        content={"applies_when": "the density stays constant", "label": "incompressible"},
        parser_confidence=0.95,
    )
    return KGGraph(nodes=[eq_node, cond_node], edges=[])


def _binary_match_credit_condition(*, entry_type, reference_nodes, student_nodes, model=None):
    """Deterministic stand-in for the real (LLM-backed) ``_batch_binary_match``:
    credits every reference "condition" entry whenever the student graph has
    at least one condition node, leaves every other binary type at "missing".
    Avoids a real OpenAI call in this DB-integration test while still
    producing a genuinely non-zero, non-fabricated rubric grade."""
    if not reference_nodes:
        return {}
    if entry_type == "condition" and student_nodes:
        return {n.node_id: {"covered": True, "confidence": 0.9} for n in reference_nodes}
    return {n.node_id: {"covered": False, "confidence": 1.0} for n in reference_nodes}


async def test_llm_canonical_composite_and_band_match_served_rubric(db_session, monkeypatch):
    """T1 regression (defect: canonical artifact built from an EMPTY coverage
    dict). ``build_llm_artifact`` was reading ``coverage["covered"]``/
    ``coverage["missing"]`` — keys the real ``compute_coverage`` return never
    has (it returns ``per_step``/``procedure_scores``/``confidences``/
    ``negotiation_counts``) — so every canonical LLM row silently graded
    ``node_coverage=0.0``/``composite=0.0``/``band="Beginning"`` no matter what
    the student actually said, while the served rubric showed a real,
    non-trivial grade. This test drives the real Done route and asserts the
    persisted canonical artifact's composite/band reflect the SAME grade the
    student was actually served, not a silently-zeroed one."""
    from apollo.projections.scorecard import _band_for, load_bands

    sid, cid, codes = await seed_course(
        db_session,
        subject_slug="fluid_mechanics",
        concept_slug="bernoulli_principle",
        problems=_INTRO,
    )
    sess, attempt = await _seed_session(db_session, current_code=codes[0], sid=sid, cid=cid)

    neo_patches = [
        patch(
            "apollo.handlers.done.KGStore.read_graph",
            new=AsyncMock(return_value=_student_graph_with_condition_credit(attempt.id)),
        ),
        patch("apollo.handlers.done.KGStore.freeze", new=AsyncMock()),
        patch("apollo.handlers.done.KGStore.stamp_graded_at", new=AsyncMock()),
        patch("apollo.overseer.coverage._batch_binary_match", new=_binary_match_credit_condition),
    ]
    out = await _run_done(
        db_session,
        sess.id,
        monkeypatch,
        neo_patches=neo_patches,
        artifact_on=True,
    )

    served_rubric_score = out["rubric"]["overall"]["score"]
    # Sanity: the fixture's seeded turn actually earns partial credit — a
    # zero-everywhere grade would make this regression test vacuous.
    assert served_rubric_score > 0

    rows = await _artifact_rows(db_session, attempt.id)
    canonical = next(r for r in rows if r.role == "canonical")
    assert canonical.grader_used == "llm_fallback"

    expected_composite = round(served_rubric_score / 100.0, 6)
    assert canonical.scores["composite"] == pytest.approx(expected_composite)
    assert canonical.scores["composite"] > 0.0
    # The node ledger must carry real entries (not the empty list a
    # covered/missing-key mismatch silently produced).
    assert canonical.node_ledger != []

    expected_band = _band_for(expected_composite, load_bands())
    assert out["scorecard"]["band"] == expected_band
    assert canonical.scores["composite"] * 100 == pytest.approx(
        out["scorecard"]["score_0_100"], abs=1
    )
