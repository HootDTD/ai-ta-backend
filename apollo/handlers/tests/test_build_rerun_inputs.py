"""WU-5B3a-0 — tests for ``done_inputs.build_rerun_inputs`` (the shared re-run
input builder the live Done path and the WU-5B3a-1 janitor both call).

Real Postgres (``db_session`` savepoint) for the session/attempt/problem rows;
Neo4j is STUBBED (``KGStore.read_graph`` / ``KGStore.read_node_graded_at`` via
``AsyncMock``) because the assertion target is the builder's keying / sourcing /
dead-letter logic — Neo4j I/O is covered green-not-skipped by the real-Neo4j
``read_node_graded_at`` suite. The builder makes NO LLM call, so none is mocked.

The #1 guard is ``test_build_rerun_inputs_keys_on_attempt_problem_id_not_current_problem_id``:
the builder MUST key ``problem_payload`` on ``attempt.problem_id``, NEVER
``sess.current_problem_id`` (which ``next.py`` advances), so the janitor rebuilds
the OLD problem the pending attempt belongs to. A single-problem fixture would
pass even with the wrong key — only the two-problem fixture catches the bug.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from unittest.mock import AsyncMock, patch

import pytest

from apollo.conftest import TEST_USER_ID
from apollo.errors import LearnerUpdateUnreconstructableError
from apollo.handlers.done_inputs import RerunInputs, build_rerun_inputs
from apollo.ontology import KGGraph, build_node
from apollo.persistence.models import (
    ApolloSession,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
)
from apollo.subjects.tests._curriculum_fixtures import seed_course

pytestmark = pytest.mark.integration

_ISO = "2026-06-18T00:00:02+00:00"


def _payload(code: str, marker: str) -> dict:
    """A minimal, DISTINCT ConceptProblem payload (id == problem_code)."""
    return {
        "id": code,
        "difficulty": "intro",
        "problem_text": f"problem {marker}",
        "marker": marker,
    }


def _good_report() -> dict:
    return {
        "narrative": "n",
        "rubric": {"overall": {"letter": "B", "score": 0.8}},
        "coverage": {},
    }


def _stub_graph(parser_confidence: float = 0.95) -> KGGraph:
    node = build_node(
        node_type="equation",
        node_id="n1",
        attempt_id=1,
        source="parser",
        content={"symbolic": "x - y", "label": "n1"},
        parser_confidence=parser_confidence,
    )
    return KGGraph(nodes=[node], edges=[])


async def _seed_session(db, *, sid, cid, current_problem_id):
    sess = ApolloSession(
        user_id=TEST_USER_ID,
        search_space_id=sid,
        concept_id=cid,
        status=SessionStatus.active.value,
        phase=SessionPhase.REPORT.value,
        current_problem_id=current_problem_id,
    )
    db.add(sess)
    await db.flush()
    return sess


async def _seed_attempt(db, *, session_id, problem_id, report):
    attempt = ProblemAttempt(
        session_id=session_id,
        problem_id=problem_id,
        difficulty="intro",
        result="graded",
        diagnostic_report=report,
    )
    db.add(attempt)
    await db.flush()
    return attempt


def _neo_stubs(*, graph=None, graded_at=None):
    """Patch the two Neo4j reads the builder makes. Returns the patch context
    managers + the AsyncMock spies."""
    graph = graph if graph is not None else _stub_graph()
    graded_at = graded_at if graded_at is not None else {"n1": _ISO}
    read_graph = AsyncMock(return_value=graph)
    read_graded = AsyncMock(return_value=graded_at)
    patches = (
        patch("apollo.handlers.done_inputs.KGStore.read_graph", new=read_graph),
        patch(
            "apollo.handlers.done_inputs.KGStore.read_node_graded_at",
            new=read_graded,
        ),
    )
    return patches, read_graph, read_graded


# ---------------------------------------------------------------------------
# #12 — THE #1 GUARD (MANDATORY, MULTI-PROBLEM)
# ---------------------------------------------------------------------------


async def test_build_rerun_inputs_keys_on_attempt_problem_id_not_current_problem_id(
    db_session,
):
    # Two problems with DISTINCT payloads.
    p_a, p_b = _payload("p_a", "AAA"), _payload("p_b", "BBB")
    sid, cid, codes = await seed_course(
        db_session,
        subject_slug="s_wprob",
        concept_slug="k_wprob",
        problems=[p_a, p_b],
    )
    assert set(codes) == {"p_a", "p_b"}
    # Fixture sanity: the two payloads DIFFER, so the test can't trivially pass.
    assert p_a != p_b

    # Attempt belongs to p_a; the session has since ADVANCED to p_b (next.py:94).
    sess = await _seed_session(db_session, sid=sid, cid=cid, current_problem_id="p_b")
    attempt = await _seed_attempt(
        db_session, session_id=sess.id, problem_id="p_a", report=_good_report()
    )

    patches, _rg, _rga = _neo_stubs()
    with patches[0], patches[1]:
        result = await build_rerun_inputs(db_session, object(), attempt=attempt, sess=sess)

    # The builder rebuilt the OLD problem (p_a) the pending attempt belongs to,
    # NOT the session's current problem (p_b).
    assert result.problem_payload == p_a
    assert result.problem_payload != p_b
    assert result.problem_payload["marker"] == "AAA"


# ---------------------------------------------------------------------------
# #13 — happy path assembles all fields
# ---------------------------------------------------------------------------


async def test_build_rerun_inputs_happy_path_assembles_all_fields(db_session):
    payload = _payload("p_h", "HHH")
    sid, cid, _codes = await seed_course(
        db_session,
        subject_slug="s_happy",
        concept_slug="k_happy",
        problems=[payload],
    )
    sess = await _seed_session(db_session, sid=sid, cid=cid, current_problem_id="p_h")
    report = _good_report()
    attempt = await _seed_attempt(db_session, session_id=sess.id, problem_id="p_h", report=report)

    graph = _stub_graph(parser_confidence=0.95)
    patches, _rg, _rga = _neo_stubs(graph=graph, graded_at={"n1": _ISO})
    with patches[0], patches[1]:
        result = await build_rerun_inputs(db_session, object(), attempt=attempt, sess=sess)

    assert isinstance(result, RerunInputs)
    assert result.problem_payload == payload
    assert result.old_rubric == report["rubric"]
    assert result.student_graph is graph
    assert result.parser_confidence == 0.95  # == min_parser_confidence_of(nodes)
    assert result.graded_at_iso == _ISO

    # RerunInputs is frozen (immutable).
    with pytest.raises(FrozenInstanceError):
        result.parser_confidence = 0.1  # type: ignore[misc]


# ---------------------------------------------------------------------------
# #14 — dead-letter: diagnostic_report is None (and NO grading reached)
# ---------------------------------------------------------------------------


async def test_build_rerun_inputs_dead_letters_when_diagnostic_report_none(db_session):
    payload = _payload("p_dr", "DRD")
    sid, cid, _codes = await seed_course(
        db_session,
        subject_slug="s_dr",
        concept_slug="k_dr",
        problems=[payload],
    )
    sess = await _seed_session(db_session, sid=sid, cid=cid, current_problem_id="p_dr")
    attempt = await _seed_attempt(db_session, session_id=sess.id, problem_id="p_dr", report=None)

    patches, read_graph, read_graded = _neo_stubs()
    with patches[0], patches[1]:
        with pytest.raises(LearnerUpdateUnreconstructableError) as exc:
            await build_rerun_inputs(db_session, object(), attempt=attempt, sess=sess)

    assert exc.value.reason == "diagnostic_report_missing"
    assert exc.value.attempt_id == attempt.id
    # The pre-flight raises BEFORE any Neo4j read / downstream grading.
    read_graph.assert_not_awaited()
    read_graded.assert_not_awaited()


# ---------------------------------------------------------------------------
# #15 — dead-letter: rubric missing / lacks "overall"
# ---------------------------------------------------------------------------


async def test_build_rerun_inputs_dead_letters_when_rubric_missing(db_session):
    sid, cid, _codes = await seed_course(
        db_session,
        subject_slug="s_rm",
        concept_slug="k_rm",
        problems=[_payload("p_rm", "RMR")],
    )
    sess = await _seed_session(db_session, sid=sid, cid=cid, current_problem_id="p_rm")

    # (a) "rubric" key absent entirely.
    attempt_no_rubric = await _seed_attempt(
        db_session,
        session_id=sess.id,
        problem_id="p_rm",
        report={"narrative": "x", "coverage": {}},
    )
    patches, _rg, _rga = _neo_stubs()
    with patches[0], patches[1]:
        with pytest.raises(LearnerUpdateUnreconstructableError) as exc_a:
            await build_rerun_inputs(db_session, object(), attempt=attempt_no_rubric, sess=sess)
    assert exc_a.value.reason == "rubric_missing"

    # (b) rubric present but lacks "overall" (calibration.py derefs ["overall"]).
    attempt_no_overall = await _seed_attempt(
        db_session,
        session_id=sess.id,
        problem_id="p_rm",
        report={"rubric": {"no_overall": 1}},
    )
    patches_b, _rg2, _rga2 = _neo_stubs()
    with patches_b[0], patches_b[1]:
        with pytest.raises(LearnerUpdateUnreconstructableError) as exc_b:
            await build_rerun_inputs(db_session, object(), attempt=attempt_no_overall, sess=sess)
    assert exc_b.value.reason == "rubric_missing"


# ---------------------------------------------------------------------------
# #16 — dead-letter: graded_at empty subgraph
# ---------------------------------------------------------------------------


async def test_build_rerun_inputs_dead_letters_when_graded_at_empty(db_session):
    sid, cid, _codes = await seed_course(
        db_session,
        subject_slug="s_ge",
        concept_slug="k_ge",
        problems=[_payload("p_ge", "GEG")],
    )
    sess = await _seed_session(db_session, sid=sid, cid=cid, current_problem_id="p_ge")
    attempt = await _seed_attempt(
        db_session, session_id=sess.id, problem_id="p_ge", report=_good_report()
    )

    # Well-formed report, but the frozen Neo4j subgraph has NO graded_at ({}).
    patches, _rg, read_graded = _neo_stubs(graded_at={})
    with patches[0], patches[1]:
        with pytest.raises(LearnerUpdateUnreconstructableError) as exc:
            await build_rerun_inputs(db_session, object(), attempt=attempt, sess=sess)

    assert exc.value.reason == "graded_at_missing"
    read_graded.assert_awaited()


# ---------------------------------------------------------------------------
# #17 — _find_problem_payload relocated, importable from BOTH modules (same object)
# ---------------------------------------------------------------------------


def test_find_problem_payload_relocated_importable_from_both_modules():
    from apollo.handlers.done import _find_problem_payload as from_done
    from apollo.handlers.done_inputs import _find_problem_payload as from_inputs

    # Single source of truth: the re-export is the SAME object (no duplicate def).
    assert from_done is from_inputs
