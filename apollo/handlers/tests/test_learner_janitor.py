"""WU-5B3a-1 — H1 drain/accounting tests for ``learner_janitor.drain_pending_attempts``.

Harness H1: the savepoint ``db_session`` fixture (real pgvector, rolled back per
test) for single-claimer drain/accounting/dead-letter/backoff/LAYER3/idempotency/
connection-drop. Neo4j is STUBBED (``KGStore.read_graph`` / ``read_node_graded_at``
+ ``write_resolution`` + ``read_node_created_at`` via ``AsyncMock``); the LLM is
STUBBED (``main_chat_adjudicator`` / ``main_chat_auditor`` deterministic, no
network). The two-connection SKIP-LOCKED proof lives in H2
(``tests/database/test_learner_janitor_contention.py``); the real-Neo4j full drain
in H3 (``tests/database/test_learner_janitor_full_drain.py``).

The drain opens its OWN ``get_async_session()`` per phase. Under ``db_session``
(one NullPool connection in an uncommitted outer transaction) those sessions must
resolve to the SAME connection the test seeds on, so we point
``get_async_session`` at a factory bound to the test's connection (the
``_drain_on_session`` shim) — this keeps the claim/work/record phases inside the
test's savepoint while still exercising the real three-session orchestration.
"""

from __future__ import annotations

import logging
import random
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select

from apollo.conftest import TEST_USER_ID, TEST_USER_ID_2
from apollo.handlers import learner_janitor
from apollo.handlers.learner_janitor import (
    BASE_DELAY_S,
    MAX_ATTEMPTS,
    MAX_DELAY_S,
    DrainResult,
    _backoff_delay_s,
    _float_env,
    _int_env,
    _truncate,
    drain_pending_attempts,
)
from apollo.ontology import KGGraph, build_node
from apollo.persistence.models import (
    ApolloSession,
    KGEntity,
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

_ISO = "2026-06-18T00:00:02+00:00"
_CONTINUITY = "eq.continuity"
_INTRO = [p for p in load_bernoulli_problem_payloads() if p["difficulty"] == "intro"]

# Sentinel so _seed_pending_attempt can distinguish "report not supplied" (use a
# good report) from "report explicitly None" (the diagnostic_report_missing case).
_UNSET = object()


# ---------------------------------------------------------------------------
# Shared deterministic stubs (mirror test_done_layer3_route_postgres.py:796-822)
# ---------------------------------------------------------------------------


def _adjudicator_stub(_request):
    return {"stu_continuity": _CONTINUITY}


def _auditor_stub(_request):
    return {"spans": {}}


def _student_graph(attempt_id: int) -> KGGraph:
    node = build_node(
        node_type="equation",
        node_id="stu_continuity",
        attempt_id=attempt_id,
        source="parser",
        content={
            "symbolic": "rho*A1*v1 - rho*A2*v2",
            "label": "continuity",
            "variables": [],
        },
        parser_confidence=0.95,
    )
    return KGGraph(nodes=[node], edges=[])


def _good_report() -> dict:
    return {
        "narrative": "n",
        "rubric": {"overall": {"letter": "B", "score": 0.8}},
        "coverage": {},
    }


def _neo_stubs(attempt_id: int, *, graph=None, graded_at=None):
    """Patch the Neo4j boundary so the H1 drain runs the full chain with no Neo4j.

    KGStore is ONE class object whether reached via done_inputs or done_grading
    (the route test confirms this), so patching the class method covers both call
    sites. Returns the list of patch context managers + the read spies.
    """
    graph = graph if graph is not None else _student_graph(attempt_id)
    graded_at = graded_at if graded_at is not None else {"stu_continuity": _ISO}
    read_graph = AsyncMock(return_value=graph)
    read_graded = AsyncMock(return_value=graded_at)
    patches = [
        patch("apollo.handlers.done_inputs.KGStore.read_graph", new=read_graph),
        patch(
            "apollo.handlers.done_inputs.KGStore.read_node_graded_at",
            new=read_graded,
        ),
        patch("apollo.handlers.done_grading.write_resolution", new=AsyncMock(return_value=None)),
        patch(
            "apollo.handlers.done_turn_order.KGStore.read_node_created_at",
            new=AsyncMock(return_value={"stu_continuity": _ISO}),
        ),
        patch("apollo.handlers.done_grading.main_chat_adjudicator", new=_adjudicator_stub),
        patch("apollo.handlers.done_grading.main_chat_auditor", new=_auditor_stub),
    ]
    return patches, read_graph, read_graded


async def _seed_pending_attempt(
    db,
    *,
    subject_slug,
    concept_slug,
    problems=None,
    user_id=TEST_USER_ID,
    diagnostic_report=_UNSET,
    problem_id=None,
    learner_update_attempts=0,
    next_attempt_at=None,
    failed_permanently=False,
):
    """Seed course/session/graded-pending-attempt/transcript-message + the
    ``eq.continuity`` KGEntity (so the covered finding maps to a learner_state).
    Returns (sess, attempt). ``problems`` defaults to the real bernoulli intro
    problem so the full chain (run_graph_simulation) has a valid payload."""
    problems = problems if problems is not None else _INTRO
    report = _good_report() if diagnostic_report is _UNSET else diagnostic_report
    sid, cid, codes = await seed_course(
        db, subject_slug=subject_slug, concept_slug=concept_slug, problems=problems
    )
    # Layer-1 entity so the resolved covered event maps to a learner_state row.
    db.add(
        KGEntity(
            concept_id=cid,
            canonical_key=_CONTINUITY,
            kind="equation",
            display_name="Continuity",
            payload={},
            aliases=[],
        )
    )
    await db.flush()
    code = problem_id if problem_id is not None else codes[0]
    sess = ApolloSession(
        user_id=user_id,
        search_space_id=sid,
        concept_id=cid,
        status=SessionStatus.active.value,
        phase=SessionPhase.REPORT.value,
        current_problem_id=code,
    )
    db.add(sess)
    await db.flush()
    attempt = ProblemAttempt(
        session_id=sess.id,
        problem_id=code,
        difficulty="intro",
        result="graded",
        diagnostic_report=report,
        learner_update_pending=True,
        learner_update_attempts=learner_update_attempts,
        learner_update_next_attempt_at=next_attempt_at,
        learner_update_failed_permanently=failed_permanently,
    )
    db.add(attempt)
    await db.flush()
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
    await db.commit()
    return sess, attempt


@asynccontextmanager
async def _bound_session(db):
    """A get_async_session() stand-in that re-uses the test's db_session connection
    (so the drain's per-phase sessions stay inside the savepoint)."""
    yield db


def _patch_session(db):
    """Patch get_async_session in the janitor module to the bound shim."""
    return patch.object(learner_janitor, "get_async_session", lambda: _bound_session(db))


async def _refresh(db, attempt_id: int) -> ProblemAttempt:
    return (
        await db.execute(
            select(ProblemAttempt)
            .where(ProblemAttempt.id == attempt_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


# ===========================================================================
# Pure unit (no DB)
# ===========================================================================


def test_backoff_delay_schedule_is_300_1200_3600():
    midpoint = lambda lo, hi: 0.0  # noqa: E731 — pin jitter to the center
    assert _backoff_delay_s(1, jitter_fn=midpoint) == 300.0
    assert _backoff_delay_s(2, jitter_fn=midpoint) == 1200.0
    assert _backoff_delay_s(3, jitter_fn=midpoint) == 3600.0
    # cap holds beyond the schedule
    assert _backoff_delay_s(4, jitter_fn=midpoint) == float(MAX_DELAY_S)
    assert _backoff_delay_s(4, jitter_fn=midpoint) == 3600.0


def test_backoff_delay_jitter_band():
    rng = random.Random(1234)
    delay = _backoff_delay_s(1, jitter_fn=rng.uniform)
    # ±10% band around BASE_DELAY_S (300): 270..330
    assert BASE_DELAY_S * 0.9 <= delay <= BASE_DELAY_S * 1.1
    assert 270.0 <= delay <= 330.0


def test_backoff_delay_uses_module_jitter_when_unpinned(monkeypatch):
    # No jitter_fn arg -> the module-level _jitter_fn is used (default path).
    monkeypatch.setattr(learner_janitor, "_jitter_fn", lambda lo, hi: 0.0)
    assert _backoff_delay_s(1) == 300.0


def test_int_env_falls_back_on_invalid(monkeypatch):
    monkeypatch.setenv("X_JANITOR_INT", "not-an-int")
    assert _int_env("X_JANITOR_INT", 7) == 7
    monkeypatch.setenv("X_JANITOR_INT", "42")
    assert _int_env("X_JANITOR_INT", 7) == 42
    monkeypatch.delenv("X_JANITOR_INT", raising=False)
    assert _int_env("X_JANITOR_INT", 7) == 7


def test_float_env_falls_back_on_invalid(monkeypatch):
    monkeypatch.setenv("X_JANITOR_FLOAT", "nope")
    assert _float_env("X_JANITOR_FLOAT", 0.25) == 0.25
    monkeypatch.setenv("X_JANITOR_FLOAT", "0.5")
    assert _float_env("X_JANITOR_FLOAT", 0.25) == 0.5
    monkeypatch.delenv("X_JANITOR_FLOAT", raising=False)
    assert _float_env("X_JANITOR_FLOAT", 0.25) == 0.25


def test_truncate_handles_none_and_caps_length():
    assert _truncate(None) is None
    assert _truncate("   ") is None  # whitespace-only collapses to empty -> None
    assert _truncate("  boom   happened ") == "boom happened"
    long = "x" * 900
    capped = _truncate(long)
    assert capped is not None and len(capped) == 500


# ===========================================================================
# H1 — drain success / accounting
# ===========================================================================


async def test_drain_success_clears_pending(db_session, monkeypatch):
    monkeypatch.setenv("APOLLO_GRAPH_SIM_SHADOW_ENABLED", "true")
    monkeypatch.setenv("APOLLO_GRAPH_SIM_LAYER3_ENABLED", "1")
    sess, attempt = await _seed_pending_attempt(
        db_session, subject_slug="fluid_mechanics", concept_slug="bernoulli_principle"
    )
    patches, _rg, _rga = _neo_stubs(attempt.id)
    [p.start() for p in patches]
    try:
        with _patch_session(db_session):
            result = await drain_pending_attempts(object(), limit=1)
    finally:
        for p in reversed(patches):
            p.stop()

    assert result == DrainResult(
        claimed=1,
        succeeded=1,
        dead_lettered=0,
        retried=0,
        deferred=0,
        backlog_remaining=0,
    )
    refreshed = await _refresh(db_session, attempt.id)
    assert refreshed.learner_update_pending is False
    assert refreshed.learner_update_failed_permanently is False
    # the belief fold actually ran -> a learner_state row exists for the user.
    ls = (
        await db_session.execute(
            select(func.count())
            .select_from(LearnerState)
            .where(LearnerState.user_id == TEST_USER_ID)
        )
    ).scalar_one()
    assert ls >= 1


async def test_drain_increments_attempts_at_claim(db_session, monkeypatch):
    monkeypatch.setenv("APOLLO_GRAPH_SIM_LAYER3_ENABLED", "1")
    sess, attempt = await _seed_pending_attempt(
        db_session, subject_slug="fm_inc", concept_slug="bp_inc"
    )
    with patch(
        "apollo.handlers.learner_janitor.run_graph_simulation",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        with _patch_session(db_session):
            result = await drain_pending_attempts(object(), limit=1)

    assert result.claimed == 1
    assert result.retried == 1
    refreshed = await _refresh(db_session, attempt.id)
    # bumped at claim even though work failed
    assert refreshed.learner_update_attempts == 1
    assert refreshed.learner_update_pending is True
    assert refreshed.learner_update_next_attempt_at is not None


# ===========================================================================
# H1 — dead-letters (terminal, no retry, no LLM)
# ===========================================================================


async def test_drain_dead_letters_on_diagnostic_report_none(db_session, monkeypatch):
    monkeypatch.setenv("APOLLO_GRAPH_SIM_LAYER3_ENABLED", "1")
    sess, attempt = await _seed_pending_attempt(
        db_session,
        subject_slug="fm_dr",
        concept_slug="bp_dr",
        diagnostic_report=None,
    )
    patches, _rg, _rga = _neo_stubs(attempt.id)
    sim_spy = AsyncMock()
    [p.start() for p in patches]
    try:
        with patch("apollo.handlers.learner_janitor.run_graph_simulation", new=sim_spy):
            with _patch_session(db_session):
                result = await drain_pending_attempts(object(), limit=1)
    finally:
        for p in reversed(patches):
            p.stop()

    assert result.dead_lettered == 1
    refreshed = await _refresh(db_session, attempt.id)
    assert refreshed.learner_update_failed_permanently is True
    assert refreshed.learner_update_failed_at is not None
    assert refreshed.learner_update_last_error == "diagnostic_report_missing"
    # no LLM / grading on the dead-letter path
    sim_spy.assert_not_awaited()


async def test_drain_dead_letters_on_rubric_missing(db_session, monkeypatch):
    monkeypatch.setenv("APOLLO_GRAPH_SIM_LAYER3_ENABLED", "1")
    sess, attempt = await _seed_pending_attempt(
        db_session,
        subject_slug="fm_rm",
        concept_slug="bp_rm",
        diagnostic_report={"narrative": "x", "coverage": {}},  # no "rubric"
    )
    patches, _rg, _rga = _neo_stubs(attempt.id)
    sim_spy = AsyncMock()
    [p.start() for p in patches]
    try:
        with patch("apollo.handlers.learner_janitor.run_graph_simulation", new=sim_spy):
            with _patch_session(db_session):
                result = await drain_pending_attempts(object(), limit=1)
    finally:
        for p in reversed(patches):
            p.stop()

    assert result.dead_lettered == 1
    refreshed = await _refresh(db_session, attempt.id)
    assert refreshed.learner_update_failed_permanently is True
    assert refreshed.learner_update_last_error == "rubric_missing"
    sim_spy.assert_not_awaited()


async def test_drain_dead_letters_on_graded_at_missing(db_session, monkeypatch):
    monkeypatch.setenv("APOLLO_GRAPH_SIM_LAYER3_ENABLED", "1")
    sess, attempt = await _seed_pending_attempt(
        db_session, subject_slug="fm_ga", concept_slug="bp_ga"
    )
    # good report, but the frozen subgraph has NO graded_at ({}).
    patches, _rg, _rga = _neo_stubs(attempt.id, graded_at={})
    sim_spy = AsyncMock()
    [p.start() for p in patches]
    try:
        with patch("apollo.handlers.learner_janitor.run_graph_simulation", new=sim_spy):
            with _patch_session(db_session):
                result = await drain_pending_attempts(object(), limit=1)
    finally:
        for p in reversed(patches):
            p.stop()

    assert result.dead_lettered == 1
    refreshed = await _refresh(db_session, attempt.id)
    assert refreshed.learner_update_failed_permanently is True
    assert refreshed.learner_update_last_error == "graded_at_missing"
    sim_spy.assert_not_awaited()


async def test_drain_dead_letters_on_naive_graded_at(db_session, monkeypatch):
    """A graded_at ISO string WITHOUT a tz offset parses tzinfo=None -> the
    defensive in-work guard dead-letters (graded_at_missing) BEFORE the belief
    Δt subtraction would TypeError. LAYER3 ON so the guard is reached."""
    monkeypatch.setenv("APOLLO_GRAPH_SIM_SHADOW_ENABLED", "true")
    monkeypatch.setenv("APOLLO_GRAPH_SIM_LAYER3_ENABLED", "1")
    sess, attempt = await _seed_pending_attempt(
        db_session, subject_slug="fm_naive", concept_slug="bp_naive"
    )
    # naive graded_at (no +00:00 offset) -> datetime.fromisoformat -> tzinfo None.
    patches, _rg, _rga = _neo_stubs(attempt.id, graded_at={"stu_continuity": "2026-06-18T00:00:02"})
    [p.start() for p in patches]
    try:
        with _patch_session(db_session):
            result = await drain_pending_attempts(object(), limit=1)
    finally:
        for p in reversed(patches):
            p.stop()

    assert result.dead_lettered == 1
    refreshed = await _refresh(db_session, attempt.id)
    assert refreshed.learner_update_failed_permanently is True
    assert refreshed.learner_update_last_error == "graded_at_missing"


async def test_drain_dead_letters_on_vanished_problem(db_session, monkeypatch):
    """A deleted ConceptProblem -> _find_problem_payload.scalar_one() raises
    NoResultFound. The janitor MUST classify it terminal (NOT crash-loop)."""
    monkeypatch.setenv("APOLLO_GRAPH_SIM_LAYER3_ENABLED", "1")
    sess, attempt = await _seed_pending_attempt(
        db_session, subject_slug="fm_vp", concept_slug="bp_vp"
    )
    # Point the attempt at a problem_code that has no ConceptProblem row.
    refreshed = await _refresh(db_session, attempt.id)
    refreshed.problem_id = "no_such_problem_code"
    await db_session.commit()

    patches, _rg, _rga = _neo_stubs(attempt.id)
    sim_spy = AsyncMock()
    [p.start() for p in patches]
    try:
        with patch("apollo.handlers.learner_janitor.run_graph_simulation", new=sim_spy):
            with _patch_session(db_session):
                result = await drain_pending_attempts(object(), limit=1)
    finally:
        for p in reversed(patches):
            p.stop()

    assert result.dead_lettered == 1
    again = await _refresh(db_session, attempt.id)
    assert again.learner_update_failed_permanently is True
    assert again.learner_update_last_error  # a reason recorded
    sim_spy.assert_not_awaited()


# ===========================================================================
# H1 — backoff schedule / MAX_ATTEMPTS
# ===========================================================================


async def test_drain_backoff_schedule_on_repeated_transient_failure(db_session, monkeypatch):
    monkeypatch.setenv("APOLLO_GRAPH_SIM_LAYER3_ENABLED", "1")
    sess, attempt = await _seed_pending_attempt(
        db_session, subject_slug="fm_bo", concept_slug="bp_bo"
    )
    midpoint = lambda lo, hi: 0.0  # noqa: E731

    async def _drain_once():
        with (
            patch(
                "apollo.handlers.learner_janitor.run_graph_simulation",
                new=AsyncMock(side_effect=RuntimeError("transient")),
            ),
            patch.object(learner_janitor, "_jitter_fn", midpoint),
        ):
            with _patch_session(db_session):
                return await drain_pending_attempts(object(), limit=1)

    # drain 1 -> attempts 1, next ~ now+300
    before1 = datetime.now(UTC)
    await _drain_once()
    a1 = await _refresh(db_session, attempt.id)
    assert a1.learner_update_attempts == 1
    assert a1.learner_update_failed_permanently is False
    _assert_delta(a1.learner_update_next_attempt_at, before1, 300)

    # re-arm (make it due) and drain 2 -> attempts 2, next ~ now+1200
    a1.learner_update_next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    before2 = datetime.now(UTC)
    await _drain_once()
    a2 = await _refresh(db_session, attempt.id)
    assert a2.learner_update_attempts == 2
    assert a2.learner_update_failed_permanently is False
    _assert_delta(a2.learner_update_next_attempt_at, before2, 1200)

    # re-arm and drain 3 -> attempts 3 == MAX_ATTEMPTS -> failed_permanently (cap path)
    a2.learner_update_next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    await _drain_once()
    a3 = await _refresh(db_session, attempt.id)
    assert a3.learner_update_attempts == 3
    assert a3.learner_update_failed_permanently is True


def _assert_delta(next_at, before, seconds, tol=5):
    assert next_at is not None
    delta = (next_at - before).total_seconds()
    assert seconds - tol <= delta <= seconds + tol, f"expected ~{seconds}s, got {delta}s"


async def test_drain_max_attempts_dead_letters(db_session, monkeypatch):
    monkeypatch.setenv("APOLLO_GRAPH_SIM_LAYER3_ENABLED", "1")
    sess, attempt = await _seed_pending_attempt(
        db_session,
        subject_slug="fm_max",
        concept_slug="bp_max",
        learner_update_attempts=2,
    )
    with patch(
        "apollo.handlers.learner_janitor.run_graph_simulation",
        new=AsyncMock(side_effect=RuntimeError("transient")),
    ):
        with _patch_session(db_session):
            result = await drain_pending_attempts(object(), limit=1)

    assert result.dead_lettered == 1
    refreshed = await _refresh(db_session, attempt.id)
    assert refreshed.learner_update_attempts == MAX_ATTEMPTS  # 2 -> 3 at claim
    assert refreshed.learner_update_failed_permanently is True
    assert refreshed.learner_update_failed_at is not None
    assert refreshed.learner_update_last_error

    # a subsequent claim no longer sees it (dead rows excluded by the predicate)
    with _patch_session(db_session):
        again = await drain_pending_attempts(object(), limit=1)
    assert again.claimed == 0


# ===========================================================================
# H1 — LAYER3-off defer
# ===========================================================================


async def test_drain_layer3_off_defers_without_belief_write(db_session, monkeypatch, caplog):
    monkeypatch.setenv("APOLLO_GRAPH_SIM_SHADOW_ENABLED", "true")
    monkeypatch.delenv("APOLLO_GRAPH_SIM_LAYER3_ENABLED", raising=False)
    sess, attempt = await _seed_pending_attempt(
        db_session, subject_slug="fm_l3", concept_slug="bp_l3"
    )
    patches, _rg, _rga = _neo_stubs(attempt.id)
    lu_spy = AsyncMock()
    [p.start() for p in patches]
    try:
        with patch("apollo.handlers.learner_janitor.run_learner_update", new=lu_spy):
            with _patch_session(db_session):
                with caplog.at_level(logging.WARNING):
                    result = await drain_pending_attempts(object(), limit=1)
    finally:
        for p in reversed(patches):
            p.stop()

    assert result.deferred == 1
    assert result.succeeded == 0
    lu_spy.assert_not_awaited()  # belief write NOT called
    refreshed = await _refresh(db_session, attempt.id)
    assert refreshed.learner_update_pending is True
    assert refreshed.learner_update_failed_permanently is False
    # the at-claim bump is undone (deferral does not consume a retry)
    assert refreshed.learner_update_attempts == 0
    assert refreshed.learner_update_next_attempt_at is None  # re-armed immediately
    # zero mastery events for the attempt
    me = (
        await db_session.execute(
            select(func.count())
            .select_from(MasteryEvent)
            .where(MasteryEvent.attempt_id == attempt.id)
        )
    ).scalar_one()
    assert me == 0
    assert any("layer3_deferred" in r.message for r in caplog.records)


# ===========================================================================
# H1 — supersede idempotency + evidence_count +1 bound
# ===========================================================================


async def test_drain_supersede_idempotent_same_attempt_twice(db_session, monkeypatch):
    monkeypatch.setenv("APOLLO_GRAPH_SIM_SHADOW_ENABLED", "true")
    monkeypatch.setenv("APOLLO_GRAPH_SIM_LAYER3_ENABLED", "1")
    sess, attempt = await _seed_pending_attempt(
        db_session, subject_slug="fm_sup", concept_slug="bp_sup"
    )

    async def _drain():
        patches, _rg, _rga = _neo_stubs(attempt.id)
        [p.start() for p in patches]
        try:
            with _patch_session(db_session):
                return await drain_pending_attempts(object(), limit=1)
        finally:
            for p in reversed(patches):
                p.stop()

    await _drain()
    states1 = (
        (await db_session.execute(select(LearnerState).where(LearnerState.user_id == TEST_USER_ID)))
        .scalars()
        .all()
    )
    assert states1, "first drain wrote at least one learner_state"
    belief1 = {s.entity_id: list(s.belief) for s in states1}
    count1 = {s.entity_id: s.evidence_count for s in states1}
    me_count1 = (
        await db_session.execute(
            select(func.count())
            .select_from(MasteryEvent)
            .where(MasteryEvent.attempt_id == attempt.id)
        )
    ).scalar_one()

    # Force the SAME attempt pending again WITHOUT clearing the prior fold
    # (the crash-between-commits window). Re-arm it due.
    refreshed = await _refresh(db_session, attempt.id)
    refreshed.learner_update_pending = True
    refreshed.learner_update_next_attempt_at = None
    refreshed.learner_update_attempts = 0
    await db_session.commit()

    await _drain()
    states2 = (
        (
            await db_session.execute(
                select(LearnerState)
                .where(LearnerState.user_id == TEST_USER_ID)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    belief2 = {s.entity_id: list(s.belief) for s in states2}
    me_count2 = (
        await db_session.execute(
            select(func.count())
            .select_from(MasteryEvent)
            .where(MasteryEvent.attempt_id == attempt.id)
        )
    ).scalar_one()

    # belief byte-identical (recompute base is the prior-attempt event log)
    assert belief2 == belief1
    # no duplicate mastery_events for (attempt, entity, kind)
    assert me_count2 == me_count1
    # evidence_count drifted by AT MOST +1 per entity (accepted crash-window bound)
    for s in states2:
        assert s.evidence_count - count1[s.entity_id] <= 1


async def test_drain_inner_reflag_accounting_is_idempotent(db_session, monkeypatch):
    """run_graph_simulation re-commits pending=True in the WORK session before
    raising (the frozen _set_pending_and_commit). The record-txn re-SELECTs by id
    and computes the correct transient outcome, attempts incremented by exactly 1."""
    monkeypatch.setenv("APOLLO_GRAPH_SIM_LAYER3_ENABLED", "1")
    sess, attempt = await _seed_pending_attempt(
        db_session, subject_slug="fm_rf", concept_slug="bp_rf"
    )

    async def _reflag_then_raise(db, neo, *, attempt, sess, **kw):
        attempt.learner_update_pending = True
        await db.commit()
        raise RuntimeError("inner reflag then boom")

    with patch(
        "apollo.handlers.learner_janitor.run_graph_simulation",
        new=AsyncMock(side_effect=_reflag_then_raise),
    ):
        with _patch_session(db_session):
            result = await drain_pending_attempts(object(), limit=1)

    assert result.retried == 1
    refreshed = await _refresh(db_session, attempt.id)
    assert refreshed.learner_update_attempts == 1  # exactly one, not two
    assert refreshed.learner_update_pending is True
    assert refreshed.learner_update_next_attempt_at is not None


# ===========================================================================
# H1 — connection-drop mid-work
# ===========================================================================


async def test_drain_connection_drop_mid_work_backs_off_cleanly(db_session, monkeypatch):
    monkeypatch.setenv("APOLLO_GRAPH_SIM_LAYER3_ENABLED", "1")
    sess, attempt = await _seed_pending_attempt(
        db_session, subject_slug="fm_cd", concept_slug="bp_cd"
    )
    # simulate the asyncpg drop AFTER the LLM stub returns (textbook class).
    with patch(
        "apollo.handlers.learner_janitor.run_graph_simulation",
        new=AsyncMock(side_effect=OSError("connection does not exist")),
    ):
        with _patch_session(db_session):
            result = await drain_pending_attempts(object(), limit=1)

    # no crash escaped; the record-txn committed a clean backoff
    assert result.retried == 1
    refreshed = await _refresh(db_session, attempt.id)
    assert refreshed.learner_update_pending is True
    assert refreshed.learner_update_failed_permanently is False
    assert refreshed.learner_update_next_attempt_at is not None


# ===========================================================================
# H1 — empty backlog / scope / skip-dead / skip-future
# ===========================================================================


async def test_drain_empty_backlog_is_noop(db_session, monkeypatch):
    monkeypatch.setenv("APOLLO_GRAPH_SIM_LAYER3_ENABLED", "1")
    sim_spy = AsyncMock()
    with patch("apollo.handlers.learner_janitor.run_graph_simulation", new=sim_spy):
        with _patch_session(db_session):
            result = await drain_pending_attempts(object(), limit=1)
    assert result == DrainResult(
        claimed=0,
        succeeded=0,
        dead_lettered=0,
        retried=0,
        deferred=0,
        backlog_remaining=0,
    )
    sim_spy.assert_not_awaited()


async def test_drain_user_id_scope_filters_other_students(db_session, monkeypatch):
    monkeypatch.setenv("APOLLO_GRAPH_SIM_LAYER3_ENABLED", "1")
    sess_a, attempt_a = await _seed_pending_attempt(
        db_session,
        subject_slug="fm_sa",
        concept_slug="bp_sa",
        user_id=TEST_USER_ID,
    )
    sess_b, attempt_b = await _seed_pending_attempt(
        db_session,
        subject_slug="fm_sb",
        concept_slug="bp_sb",
        user_id=TEST_USER_ID_2,
    )
    patches, _rg, _rga = _neo_stubs(attempt_a.id)
    [p.start() for p in patches]
    try:
        with _patch_session(db_session):
            result = await drain_pending_attempts(object(), limit=5, user_id=TEST_USER_ID)
    finally:
        for p in reversed(patches):
            p.stop()

    assert result.claimed == 1  # only A's attempt
    a = await _refresh(db_session, attempt_a.id)
    b = await _refresh(db_session, attempt_b.id)
    assert a.learner_update_pending is False  # worked
    assert b.learner_update_pending is True  # untouched
    assert b.learner_update_attempts == 0


async def test_drain_skips_already_dead_lettered_rows(db_session, monkeypatch):
    monkeypatch.setenv("APOLLO_GRAPH_SIM_LAYER3_ENABLED", "1")
    sess, attempt = await _seed_pending_attempt(
        db_session,
        subject_slug="fm_sd",
        concept_slug="bp_sd",
        failed_permanently=True,
    )
    sim_spy = AsyncMock()
    with patch("apollo.handlers.learner_janitor.run_graph_simulation", new=sim_spy):
        with _patch_session(db_session):
            result = await drain_pending_attempts(object(), limit=1)
    assert result.claimed == 0
    sim_spy.assert_not_awaited()


async def test_drain_skips_future_next_attempt_at(db_session, monkeypatch):
    monkeypatch.setenv("APOLLO_GRAPH_SIM_LAYER3_ENABLED", "1")
    future = datetime.now(UTC) + timedelta(hours=1)
    sess, attempt = await _seed_pending_attempt(
        db_session,
        subject_slug="fm_fu",
        concept_slug="bp_fu",
        next_attempt_at=future,
    )
    # not yet due -> skipped
    with _patch_session(db_session):
        first = await drain_pending_attempts(object(), limit=1)
    assert first.claimed == 0

    # set it to the past -> claimed
    refreshed = await _refresh(db_session, attempt.id)
    refreshed.learner_update_next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    patches, _rg, _rga = _neo_stubs(attempt.id)
    [p.start() for p in patches]
    try:
        with _patch_session(db_session):
            second = await drain_pending_attempts(object(), limit=1)
    finally:
        for p in reversed(patches):
            p.stop()
    assert second.claimed == 1
