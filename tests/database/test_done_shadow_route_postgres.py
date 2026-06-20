"""WU-4C1 — real-PG gate: the Done shadow chain end-to-end on pgvector.

Drives ``handle_done`` with ``APOLLO_GRAPH_SIM_SHADOW_ENABLED`` ON against the
real ``db_session`` (Base.metadata.create_all, savepoint rollback). The bernoulli
``ConceptProblem.payload`` (carrying declared_paths / symbolic_mappings /
entity_key) is REAL via the ``seed_course`` harness, so the raw-payload chain
exercises the genuine §6.4 grading core. The Neo4j boundary calls
(``read_graph`` / ``freeze`` / ``stamp_graded_at`` / ``write_resolution`` /
``read_node_created_at``) are stubbed; the LLM adjudicator + auditor are injected
deterministic stubs. The graph-compare / grading callees run REAL (pure).

These MUST RUN GREEN (not skip) with Docker up — a skip is a FAIL of the gate.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select

from apollo.conftest import TEST_USER_ID
from apollo.handlers.done import handle_done
from apollo.ontology import KGGraph, build_node
from apollo.persistence.models import (
    ApolloSession,
    GraphComparisonRun,
    LearnerState,
    MasteryEvent,
    Message,
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
    """A tiny real student graph (one equation node) so the chain has something
    to resolve / canonicalize / grade. node_id mirrors the parser's stu_ prefix."""
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
    sess = ApolloSession(
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
    # one student message so the turn_order PG read has a real row
    db.add(
        Message(
            session_id=sess.id,
            attempt_id=attempt.id,
            role="student",
            content="continuity says rho A1 v1 equals rho A2 v2",
            turn_index=0,
        )
    )
    await db.flush()
    return sess, attempt


def _adjudicator_stub(_request):
    """Deterministic resolver adjudicator: resolve the student node to the
    continuity entity key (in the closed candidate set)."""
    return {"stu_continuity": "eq.continuity"}


def _auditor_stub(_request):
    """Deterministic transcript auditor: find nothing (empty spans)."""
    return {"spans": {}}


def _neo_stubs(attempt_id: int):
    """Patch the Neo4j-touching boundary calls so no container is needed in the
    PG gate (the Neo4j gate is a separate file). read_graph returns the student
    graph; write_resolution / read_node_created_at are no-op stubs."""
    return [
        patch(
            "apollo.handlers.done.KGStore.read_graph",
            new=AsyncMock(return_value=_student_graph(attempt_id)),
        ),
        patch("apollo.handlers.done.KGStore.freeze", new=AsyncMock()),
        patch("apollo.handlers.done.KGStore.stamp_graded_at", new=AsyncMock()),
        # WU-5B3a-0: the shadow chain now sources problem_payload via
        # build_rerun_inputs, which reads the Neo4j graded_at; stub it non-empty so
        # the live path never dead-letters (same KGStore class object whether
        # reached via done.py or done_inputs.py).
        patch("apollo.handlers.done.KGStore.read_node_graded_at",
              new=AsyncMock(return_value={"stu_continuity": "2026-06-18T00:00:02+00:00"})),
        patch("apollo.handlers.done_grading.write_resolution",
              new=AsyncMock(return_value=None)),
        patch("apollo.handlers.done_turn_order.KGStore.read_node_created_at",
              new=AsyncMock(return_value={"stu_continuity": "2026-06-18T00:00:02+00:00"})),
        patch("apollo.handlers.done_grading.main_chat_adjudicator", new=_adjudicator_stub),
        patch("apollo.handlers.done_grading.main_chat_auditor", new=_auditor_stub),
    ]


def _start(patches):
    for p in patches:
        p.start()


def _stop(patches):
    for p in reversed(patches):
        p.stop()


async def _run_done(db, sess_id, monkeypatch, *, neo_patches):
    monkeypatch.setenv("APOLLO_GRAPH_SIM_SHADOW_ENABLED", "true")
    _start(neo_patches)
    try:
        return await handle_done(db=db, neo=object(), session_id=sess_id)
    finally:
        _stop(neo_patches)


async def test_shadow_on_persists_run_and_findings_old_grade_unchanged(db_session, monkeypatch):
    """LOAD-BEARING #2: flag on, happy path -> a comparison run row exists for
    the attempt; the returned dict's rubric/coverage are OLD-path values; and the
    4C/5A boundary holds: apollo_mastery_events + apollo_learner_state are EMPTY."""
    sid, cid, codes = await seed_course(
        db_session,
        subject_slug="fluid_mechanics",
        concept_slug="bernoulli_principle",
        problems=_INTRO,
    )
    sess, attempt = await _seed_session(db_session, current_code=codes[0], sid=sid, cid=cid)

    out = await _run_done(db_session, sess.id, monkeypatch, neo_patches=_neo_stubs(attempt.id))

    # OLD-path student-facing payload is present + well-formed
    assert "rubric" in out and "coverage" in out and "xp_earned" in out

    # a comparison run row exists for this attempt (the shadow persist)
    run_count = (
        await db_session.execute(
            select(func.count())
            .select_from(GraphComparisonRun)
            .where(GraphComparisonRun.attempt_id == attempt.id)
        )
    ).scalar_one()
    assert run_count == 1

    # the 4C/5A boundary: NO mastery events / learner state written
    me = (
        await db_session.execute(
            select(func.count())
            .select_from(MasteryEvent)
            .where(MasteryEvent.user_id == TEST_USER_ID)
        )
    ).scalar_one()
    ls = (
        await db_session.execute(
            select(func.count())
            .select_from(LearnerState)
            .where(LearnerState.user_id == TEST_USER_ID)
        )
    ).scalar_one()
    assert me == 0
    assert ls == 0

    # the OLD grade is committed, not voided
    refreshed = (
        await db_session.execute(select(ProblemAttempt).where(ProblemAttempt.id == attempt.id))
    ).scalar_one()
    assert refreshed.result == "graded"
    assert refreshed.learner_update_pending is False


async def test_reference_invalid_returns_409_old_grade_committed(db_session, monkeypatch):
    """LOAD-BEARING #3: a payload whose reference fails validation ->
    ReferenceGraphInvalidError surfaces, the OLD grade is still committed
    (result='graded'), learner_update_pending is False, and NO run row written."""
    # drop declared_paths so build_reference_canonical's validate gate fails
    broken = [dict(p) for p in _INTRO]
    broken[0] = {k: v for k, v in broken[0].items() if k != "declared_paths"}
    sid, cid, codes = await seed_course(
        db_session,
        subject_slug="fluid_mechanics",
        concept_slug="bernoulli_principle",
        problems=broken,
    )
    sess, attempt = await _seed_session(db_session, current_code=codes[0], sid=sid, cid=cid)

    from apollo.graph_compare.validator import ReferenceGraphInvalidError

    with pytest.raises(ReferenceGraphInvalidError):
        await _run_done(db_session, sess.id, monkeypatch, neo_patches=_neo_stubs(attempt.id))

    refreshed = (
        await db_session.execute(select(ProblemAttempt).where(ProblemAttempt.id == attempt.id))
    ).scalar_one()
    assert refreshed.result == "graded"
    # reference-invalid is raised BEFORE any cross-store write -> pending NOT set
    assert refreshed.learner_update_pending is False
    run_count = (
        await db_session.execute(
            select(func.count())
            .select_from(GraphComparisonRun)
            .where(GraphComparisonRun.attempt_id == attempt.id)
        )
    ).scalar_one()
    assert run_count == 0


async def test_resolution_unavailable_503_pending_grade_committed(db_session, monkeypatch):
    """LOAD-BEARING #4: write_resolution raises ResolutionUnavailableError ->
    the error surfaces, attempt.learner_update_pending is True in the DB, and the
    OLD grade is committed (result='graded')."""
    from apollo.errors import ResolutionUnavailableError

    sid, cid, codes = await seed_course(
        db_session,
        subject_slug="fluid_mechanics",
        concept_slug="bernoulli_principle",
        problems=_INTRO,
    )
    sess, attempt = await _seed_session(db_session, current_code=codes[0], sid=sid, cid=cid)

    patches = _neo_stubs(attempt.id)
    # override write_resolution to raise the infra error
    patches = [p for p in patches if "write_resolution" not in str(p)]
    patches.append(
        patch(
            "apollo.handlers.done_grading.write_resolution",
            new=AsyncMock(
                side_effect=ResolutionUnavailableError(
                    stage="write_resolves_to", last_error="neo4j down"
                )
            ),
        )
    )

    with pytest.raises(ResolutionUnavailableError):
        await _run_done(db_session, sess.id, monkeypatch, neo_patches=patches)

    refreshed = (
        await db_session.execute(select(ProblemAttempt).where(ProblemAttempt.id == attempt.id))
    ).scalar_one()
    assert refreshed.result == "graded"
    assert refreshed.learner_update_pending is True


async def test_retry_supersedes_run_idempotent(db_session, monkeypatch):
    """LOAD-BEARING #5 (PG half): running the shadow chain twice for the same
    attempt leaves exactly ONE comparison run row (persist supersede; no UNIQUE
    crash)."""
    sid, cid, codes = await seed_course(
        db_session,
        subject_slug="fluid_mechanics",
        concept_slug="bernoulli_principle",
        problems=_INTRO,
    )
    sess, attempt = await _seed_session(db_session, current_code=codes[0], sid=sid, cid=cid)

    await _run_done(db_session, sess.id, monkeypatch, neo_patches=_neo_stubs(attempt.id))
    # second Done on the same attempt (re-grade) -> supersede
    await _run_done(db_session, sess.id, monkeypatch, neo_patches=_neo_stubs(attempt.id))

    run_count = (
        await db_session.execute(
            select(func.count())
            .select_from(GraphComparisonRun)
            .where(GraphComparisonRun.attempt_id == attempt.id)
        )
    ).scalar_one()
    assert run_count == 1


async def test_turn_order_query_runs_on_real_pg(db_session, monkeypatch):
    """build_turn_order joins the real apollo_messages.turn_index for the attempt.
    Two student messages at distinct times + stubbed node created_at -> the map
    binds each node to the nearest at-or-before student turn_index."""
    from apollo.handlers.done_turn_order import build_turn_order

    sid, cid, codes = await seed_course(
        db_session,
        subject_slug="fluid_mechanics",
        concept_slug="bernoulli_principle",
        problems=_INTRO,
    )
    sess, attempt = await _seed_session(db_session, current_code=codes[0], sid=sid, cid=cid)
    # add a second, later student message (turn 1)
    db_session.add(
        Message(
            session_id=sess.id,
            attempt_id=attempt.id,
            role="student",
            content="and bernoulli relates the pressures",
            turn_index=1,
        )
    )
    await db_session.flush()

    # two node created_at values: one early (turn 0 window), one late (turn 1)
    node_created = {
        "n_early": "2026-06-18T00:00:05+00:00",
        "n_late": "2026-06-18T00:30:00+00:00",
    }
    with patch(
        "apollo.handlers.done_turn_order.KGStore.read_node_created_at",
        new=AsyncMock(return_value=node_created),
    ):
        out = await build_turn_order(
            db_session, object(), attempt_id=attempt.id, student_graph=KGGraph()
        )
    # monotone: the early node's position <= the late node's position
    assert out["n_early"] <= out["n_late"]
    assert set(out) == {"n_early", "n_late"}
