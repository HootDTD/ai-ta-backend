"""WU-3B2h — real-PG gate: the §8B.3 quarantine sweep, the selector exclusion,
and the §9 SPEC-4 shadow-verification invariant (the spec-closing test).

Three concerns, one file, all on the savepoint ``db_session`` fixture (real
pgvector, ``create_all``, rolled back per test). NO LLM, NO Neo4j for the sweep
tests (the sweep is pure-Postgres); the ONE shadow test mirrors the proven
``test_done_shadow_route_postgres.py`` harness (Neo4j/LLM boundary stubs) to drive
``handle_done`` with ``APOLLO_GRAPH_SIM_SHADOW_ENABLED`` ON and
``APOLLO_GRAPH_SIM_LAYER3_ENABLED`` OFF.

These MUST RUN GREEN (not skip) with Docker up — a skip is a FAIL of the gate.

Layer 2 (sweep + selector):
  * the findings->runs->attempts TEXT-problem_code -> BIGINT concept_problems
    resolution writes ``quarantined_at`` on the RIGHT row, course-scoped by
    ``runs.search_space_id``;
  * the reversible re-clear path (concentration no longer fires -> back to NULL);
  * the ``missing_node`` filter + audit logs are load-bearing (mutation-proven);
  * a quarantined Tier-2 problem is EXCLUDED by ``list_problems_for_concept``.

Layer 3 (the shadow invariant): SHADOW ON + LAYER3 OFF -> an auto-provisioned-
shaped Tier-2 problem is teachable, produces ``apollo_graph_comparison_findings``
(so quarantine has a data source) AND moves ZERO Layer-3 beliefs
(``apollo_mastery_events`` + ``apollo_learner_state`` UNCHANGED).
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select

from apollo.conftest import TEST_USER_ID
from apollo.handlers.done import handle_done
from apollo.overseer.problem_selector import list_problems_for_concept
from apollo.ontology import KGGraph, build_node
from apollo.persistence.models import (
    ApolloSession,
    ConceptProblem,
    GraphComparisonFinding,
    GraphComparisonRun,
    LearnerState,
    MasteryEvent,
    Message,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
)
from apollo.provisioning.quarantine import _node_key, sweep_quarantine
from apollo.subjects.tests._curriculum_fixtures import (
    load_bernoulli_problem_payloads,
    seed_concept,
    seed_course,
    seed_search_space,
)

pytestmark = pytest.mark.integration

_INTRO = [p for p in load_bernoulli_problem_payloads() if p["difficulty"] == "intro"]


# ===========================================================================
# Layer-2 seed helpers — pure ORM. Build findings->runs->attempts so the sweep
# has a real join surface; set ConceptProblem.search_space_id so the resolution
# join is course-scoped (seed_course leaves it NULL).
# ===========================================================================


async def _seed_problem_row(
    db, *, concept_id: int, search_space_id: int, problem_code: str
) -> ConceptProblem:
    """One teachable (Tier-2) concept_problems row with search_space_id set."""
    row = ConceptProblem(
        concept_id=concept_id,
        problem_code=problem_code,
        difficulty="intro",
        payload={"id": problem_code, "difficulty": "intro"},
        tier=2,
        search_space_id=search_space_id,
    )
    db.add(row)
    await db.flush()
    return row


async def _shared_session(db, *, search_space_id: int, concept_id: int) -> ApolloSession:
    # status='ended' (NOT 'active'): the partial unique index
    # ix_apollo_sessions_unique_active_per_user only covers active sessions, so a
    # test may seed several of these per user. The sweep never reads the session.
    sess = ApolloSession(
        user_id=TEST_USER_ID,
        search_space_id=search_space_id,
        concept_id=concept_id,
        status=SessionStatus.ended.value,
        phase=SessionPhase.SOLVING.value,
    )
    db.add(sess)
    await db.flush()
    return sess


async def _seed_run(db, *, attempt_id: int, search_space_id: int) -> GraphComparisonRun:
    run = GraphComparisonRun(
        attempt_id=attempt_id,
        user_id=TEST_USER_ID,
        search_space_id=search_space_id,
        coverage_score=0.5,
        soundness_score=0.5,
        bisimilarity_score=0.5,
        normalization_confidence=0.9,
        comparison_version="v1",
        reference_graph_hash="hash",
    )
    db.add(run)
    await db.flush()
    return run


async def _seed_attempts_with_findings(
    db,
    *,
    session_id: int,
    search_space_id: int,
    problem_code: str,
    n_attempts: int,
    miss_plan,
    finding_kind: str = "missing_node",
):
    """Seed ``n_attempts`` graded attempts of ``problem_code``; for attempt index
    i, write a ``finding_kind`` finding for each node key in ``miss_plan(i)``.

    ``miss_plan`` is a callable ``i -> iterable[str]`` (the canonical reference
    node keys the i-th attempt "missed"). The node identity is carried in
    ``reference_node_ids`` (a JSONB list) — the field the PRODUCTION grading core
    actually populates for a ``missing_node`` finding (``entity_id`` is hardcoded
    NULL in v1, see ``apollo/grading/persistence.py`` finding_to_row_spec). One
    GraphComparisonRun per attempt (N graded attempts == N runs), keyed by
    ``search_space_id``.
    """
    for i in range(n_attempts):
        attempt = ProblemAttempt(
            session_id=session_id, problem_id=problem_code, difficulty="intro"
        )
        db.add(attempt)
        await db.flush()
        run = await _seed_run(
            db, attempt_id=attempt.id, search_space_id=search_space_id
        )
        for node_key in miss_plan(i):
            db.add(
                GraphComparisonFinding(
                    run_id=run.id,
                    finding_kind=finding_kind,
                    reference_node_ids=[node_key],
                )
            )
    await db.flush()


# ===========================================================================
# Layer 2 — the real-PG sweep
# ===========================================================================


async def test_sweep_quarantines_concentrated_problem_on_right_bigint_row(db_session):
    """P: N=10, node ``n_bad`` missed in 9 attempts, two others missed rarely ->
    the BIGINT concept_problems row for ``(concept_id, problem_code)`` gets
    ``quarantined_at`` set; report.n_quarantined == 1 with P in fired_problem_codes.
    Proves the TEXT->BIGINT resolution writes the right row (§9 OPS-3)."""
    sid = await seed_search_space(db_session)
    cid = await seed_concept(
        db_session, search_space_id=sid, subject_slug="s_q", concept_slug="c_q"
    )
    p = await _seed_problem_row(
        db_session, concept_id=cid, search_space_id=sid, problem_code="P-CONC"
    )
    sess = await _shared_session(db_session, search_space_id=sid, concept_id=cid)

    def plan(i):
        misses = ["n_bad"] if i < 9 else []  # 9/10 miss n_bad
        if i == 0:
            misses = misses + ["n_a"]  # one stray miss of n_a
        return misses

    await _seed_attempts_with_findings(
        db_session, session_id=sess.id, search_space_id=sid,
        problem_code="P-CONC", n_attempts=10, miss_plan=plan,
    )

    report = await sweep_quarantine(db_session, search_space_id=sid)

    assert report.n_quarantined == 1
    assert "P-CONC" in report.fired_problem_codes
    refreshed = (await db_session.execute(
        select(ConceptProblem).where(ConceptProblem.id == p.id)
    )).scalar_one()
    assert refreshed.quarantined_at is not None


async def test_sweep_does_not_quarantine_uniform_or_sparse_problem(db_session):
    """Q (N=10, uniform misses -> concentration < margin) and R (N=4 < N_MIN)
    stay live; only the concentrated P fires."""
    sid = await seed_search_space(db_session)
    cid = await seed_concept(
        db_session, search_space_id=sid, subject_slug="s_u", concept_slug="c_u"
    )
    p = await _seed_problem_row(db_session, concept_id=cid, search_space_id=sid, problem_code="P")
    q = await _seed_problem_row(db_session, concept_id=cid, search_space_id=sid, problem_code="Q")
    r = await _seed_problem_row(db_session, concept_id=cid, search_space_id=sid, problem_code="R")
    sess = await _shared_session(db_session, search_space_id=sid, concept_id=cid)

    # P concentrated: p_bad 9/10, p_a 1/10 -> mean pulled down, concentration high.
    await _seed_attempts_with_findings(
        db_session, session_id=sess.id, search_space_id=sid, problem_code="P",
        n_attempts=10, miss_plan=lambda i: (["p_bad"] if i < 9 else []) + (["p_a"] if i == 0 else []),
    )
    # Q uniform-hard: two nodes each missed 8/10 -> concentration ~0 < margin
    await _seed_attempts_with_findings(
        db_session, session_id=sess.id, search_space_id=sid, problem_code="Q",
        n_attempts=10, miss_plan=lambda i: (["q_x"] if i < 8 else []) + (["q_y"] if i < 8 else []),
    )
    # R sparse: N=4 < N_MIN, even though concentrated
    await _seed_attempts_with_findings(
        db_session, session_id=sess.id, search_space_id=sid, problem_code="R",
        n_attempts=4, miss_plan=lambda i: ["r_bad"] if i < 4 else [],
    )

    report = await sweep_quarantine(db_session, search_space_id=sid)

    assert report.n_quarantined == 1
    assert report.fired_problem_codes == ("P",)
    for row in (p, q, r):
        refreshed = (await db_session.execute(
            select(ConceptProblem).where(ConceptProblem.id == row.id)
        )).scalar_one()
        if row.problem_code == "P":
            assert refreshed.quarantined_at is not None
        else:
            assert refreshed.quarantined_at is None


async def test_sweep_is_course_scoped_by_run_search_space_id(db_session):
    """The SAME problem_code string under TWO courses, concentrated-missing in
    course A only; sweep(search_space_id=A) quarantines only A's BIGINT row.
    Proves runs.search_space_id course scoping + the (search_space_id,
    problem_code) resolution — no cross-course leak."""
    sid_a = await seed_search_space(db_session)
    sid_b = await seed_search_space(db_session)
    cid_a = await seed_concept(db_session, search_space_id=sid_a, subject_slug="s_a", concept_slug="c_a")
    cid_b = await seed_concept(db_session, search_space_id=sid_b, subject_slug="s_b", concept_slug="c_b")
    row_a = await _seed_problem_row(db_session, concept_id=cid_a, search_space_id=sid_a, problem_code="SHARED")
    row_b = await _seed_problem_row(db_session, concept_id=cid_b, search_space_id=sid_b, problem_code="SHARED")
    sess_a = await _shared_session(db_session, search_space_id=sid_a, concept_id=cid_a)
    sess_b = await _shared_session(db_session, search_space_id=sid_b, concept_id=cid_b)

    # Course A: concentrated on bad_a (9/10), low_a rarely (0) -> mean pulled down.
    # Course B: also concentrated but a DIFFERENT course (must stay untouched).
    await _seed_attempts_with_findings(
        db_session, session_id=sess_a.id, search_space_id=sid_a, problem_code="SHARED",
        n_attempts=10, miss_plan=lambda i: (["bad_a"] if i < 9 else []) + (["low_a"] if i == 0 else []),
    )
    await _seed_attempts_with_findings(
        db_session, session_id=sess_b.id, search_space_id=sid_b, problem_code="SHARED",
        n_attempts=10, miss_plan=lambda i: (["bad_b"] if i < 9 else []) + (["low_b"] if i == 0 else []),
    )

    report = await sweep_quarantine(db_session, search_space_id=sid_a)

    assert report.n_quarantined == 1
    refreshed_a = (await db_session.execute(
        select(ConceptProblem).where(ConceptProblem.id == row_a.id)
    )).scalar_one()
    refreshed_b = (await db_session.execute(
        select(ConceptProblem).where(ConceptProblem.id == row_b.id)
    )).scalar_one()
    assert refreshed_a.quarantined_at is not None
    assert refreshed_b.quarantined_at is None  # course B untouched


async def test_sweep_reclears_when_concentration_no_longer_fires(db_session):
    """REVERSIBILITY: P is ALREADY quarantined, but the CURRENT findings no longer
    concentrate (uniform-hard). The sweep clears P back to NULL and reports it in
    cleared_problem_codes (ADJ #1 mitigation a)."""
    sid = await seed_search_space(db_session)
    cid = await seed_concept(db_session, search_space_id=sid, subject_slug="s_rc", concept_slug="c_rc")
    p = await _seed_problem_row(db_session, concept_id=cid, search_space_id=sid, problem_code="P-RC")
    # Pre-quarantine it directly (a prior sweep fired).
    from datetime import UTC, datetime
    p.quarantined_at = datetime.now(UTC)
    await db_session.flush()

    sess = await _shared_session(db_session, search_space_id=sid, concept_id=cid)
    # Now uniform-hard (each missed 8/10) -> concentration < margin -> does NOT fire.
    await _seed_attempts_with_findings(
        db_session, session_id=sess.id, search_space_id=sid, problem_code="P-RC",
        n_attempts=10, miss_plan=lambda i: (["x"] if i < 8 else []) + (["y"] if i < 8 else []),
    )

    report = await sweep_quarantine(db_session, search_space_id=sid)

    assert report.n_cleared == 1
    assert "P-RC" in report.cleared_problem_codes
    refreshed = (await db_session.execute(
        select(ConceptProblem).where(ConceptProblem.id == p.id)
    )).scalar_one()
    assert refreshed.quarantined_at is None


async def test_sweep_missing_node_filter_is_load_bearing(db_session):
    """MUTATION-PROOF the join filter: P's concentrated node carries
    finding_kind='covered_node' (NOT missing_node). The sweep counts ONLY
    missing_node findings, so P is NOT quarantined. Dropping the
    WHERE finding_kind='missing_node' filter would count covered nodes as misses
    -> P would wrongly fire."""
    sid = await seed_search_space(db_session)
    cid = await seed_concept(db_session, search_space_id=sid, subject_slug="s_mn", concept_slug="c_mn")
    p = await _seed_problem_row(db_session, concept_id=cid, search_space_id=sid, problem_code="P-MN")
    sess = await _shared_session(db_session, search_space_id=sid, concept_id=cid)
    # 10 attempts. 'covered' is missed-but-COVERED-kind 9/10 (a covered_node
    # finding, NOT a miss). 'other' carries a real missing_node finding 1/10.
    # With the missing_node filter ON: only 'other' counts (1/10) -> no fire.
    # Dropping the filter would count 'covered' (9/10) against 'other' (1/10) ->
    # concentration crosses the margin -> P would WRONGLY fire (the mutation).
    for i in range(10):
        attempt = ProblemAttempt(session_id=sess.id, problem_id="P-MN", difficulty="intro")
        db_session.add(attempt)
        await db_session.flush()
        run = await _seed_run(db_session, attempt_id=attempt.id, search_space_id=sid)
        if i < 9:
            db_session.add(GraphComparisonFinding(
                run_id=run.id, reference_node_ids=["covered"], finding_kind="covered_node"))
        if i == 0:
            db_session.add(GraphComparisonFinding(
                run_id=run.id, reference_node_ids=["other"], finding_kind="missing_node"))
    await db_session.flush()

    report = await sweep_quarantine(db_session, search_space_id=sid)

    assert report.n_quarantined == 0
    refreshed = (await db_session.execute(
        select(ConceptProblem).where(ConceptProblem.id == p.id)
    )).scalar_one()
    assert refreshed.quarantined_at is None


async def test_sweep_ignores_empty_reference_node_ids_findings(db_session):
    """A finding with an EMPTY ``reference_node_ids`` carries no resolvable node
    identity (a pruned/unkeyed reference node); the sweep must exclude it from the
    per-node aggregation so it never becomes a phantom node. Here 9/10 of P's
    missing_node findings carry ``reference_node_ids=[]`` and only 1/10 carries a
    real key -> no node concentrates -> P is NOT quarantined.

    Dropping the empty-key guard would let the unkeyed findings collapse into one
    phantom node missed 9/10 against 'low' (1/10) -> concentration crosses margin
    -> P would WRONGLY fire (the mutation)."""
    sid = await seed_search_space(db_session)
    cid = await seed_concept(db_session, search_space_id=sid, subject_slug="s_nl", concept_slug="c_nl")
    p = await _seed_problem_row(db_session, concept_id=cid, search_space_id=sid, problem_code="P-NULL")
    sess = await _shared_session(db_session, search_space_id=sid, concept_id=cid)
    for i in range(10):
        attempt = ProblemAttempt(session_id=sess.id, problem_id="P-NULL", difficulty="intro")
        db_session.add(attempt)
        await db_session.flush()
        run = await _seed_run(db_session, attempt_id=attempt.id, search_space_id=sid)
        if i < 9:
            db_session.add(GraphComparisonFinding(
                run_id=run.id, reference_node_ids=[], finding_kind="missing_node"))
        if i == 0:
            db_session.add(GraphComparisonFinding(
                run_id=run.id, reference_node_ids=["low"], finding_kind="missing_node"))
    await db_session.flush()

    report = await sweep_quarantine(db_session, search_space_id=sid)

    assert report.n_quarantined == 0
    refreshed = (await db_session.execute(
        select(ConceptProblem).where(ConceptProblem.id == p.id)
    )).scalar_one()
    assert refreshed.quarantined_at is None


async def test_sweep_emits_audit_log_on_fire(db_session, caplog):
    """The per-fire observability row exists for human audit: exactly one
    event=quarantine_fire record carrying problem_code, search_space_id,
    node_key, n_attempts, top_miss_rate, concentration in extra."""
    sid = await seed_search_space(db_session)
    cid = await seed_concept(db_session, search_space_id=sid, subject_slug="s_lg", concept_slug="c_lg")
    await _seed_problem_row(db_session, concept_id=cid, search_space_id=sid, problem_code="P-LOG")
    sess = await _shared_session(db_session, search_space_id=sid, concept_id=cid)
    await _seed_attempts_with_findings(
        db_session, session_id=sess.id, search_space_id=sid, problem_code="P-LOG",
        n_attempts=10, miss_plan=lambda i: (["bad"] if i < 9 else []) + (["low"] if i == 0 else []),
    )

    with caplog.at_level(logging.INFO, logger="apollo.provisioning.quarantine"):
        await sweep_quarantine(db_session, search_space_id=sid)

    fires = [r for r in caplog.records if getattr(r, "event", None) == "quarantine_fire"]
    assert len(fires) == 1
    rec = fires[0]
    assert rec.problem_code == "P-LOG"
    assert rec.search_space_id == sid
    assert rec.node_key == _node_key(["bad"])
    assert rec.n_attempts == 10
    assert rec.top_miss_rate == pytest.approx(0.9)
    assert rec.concentration >= 0.40


async def test_sweep_emits_clear_log_on_reclear(db_session, caplog):
    """The reclear path emits exactly one event=quarantine_clear record."""
    sid = await seed_search_space(db_session)
    cid = await seed_concept(db_session, search_space_id=sid, subject_slug="s_cl", concept_slug="c_cl")
    p = await _seed_problem_row(db_session, concept_id=cid, search_space_id=sid, problem_code="P-CL")
    from datetime import UTC, datetime
    p.quarantined_at = datetime.now(UTC)
    await db_session.flush()
    sess = await _shared_session(db_session, search_space_id=sid, concept_id=cid)
    await _seed_attempts_with_findings(
        db_session, session_id=sess.id, search_space_id=sid, problem_code="P-CL",
        n_attempts=10, miss_plan=lambda i: (["x"] if i < 8 else []) + (["y"] if i < 8 else []),
    )

    with caplog.at_level(logging.INFO, logger="apollo.provisioning.quarantine"):
        await sweep_quarantine(db_session, search_space_id=sid)

    clears = [r for r in caplog.records if getattr(r, "event", None) == "quarantine_clear"]
    assert len(clears) == 1
    assert clears[0].problem_code == "P-CL"


async def test_sweep_idempotent_second_run_no_change(db_session):
    """No double-stamp: the second sweep over an already-fired state reports
    n_quarantined == 0 and leaves quarantined_at unchanged."""
    sid = await seed_search_space(db_session)
    cid = await seed_concept(db_session, search_space_id=sid, subject_slug="s_id", concept_slug="c_id")
    p = await _seed_problem_row(db_session, concept_id=cid, search_space_id=sid, problem_code="P-ID")
    sess = await _shared_session(db_session, search_space_id=sid, concept_id=cid)
    await _seed_attempts_with_findings(
        db_session, session_id=sess.id, search_space_id=sid, problem_code="P-ID",
        n_attempts=10, miss_plan=lambda i: (["bad"] if i < 9 else []) + (["low"] if i == 0 else []),
    )

    first = await sweep_quarantine(db_session, search_space_id=sid)
    assert first.n_quarantined == 1
    stamped = (await db_session.execute(
        select(ConceptProblem).where(ConceptProblem.id == p.id)
    )).scalar_one().quarantined_at
    assert stamped is not None

    second = await sweep_quarantine(db_session, search_space_id=sid)
    assert second.n_quarantined == 0
    after = (await db_session.execute(
        select(ConceptProblem).where(ConceptProblem.id == p.id)
    )).scalar_one().quarantined_at
    assert after == stamped  # no re-stamp


async def test_sweep_resolution_miss_is_logged_and_skipped(db_session, caplog):
    """An orphan finding (a problem_code with no matching concept_problems row)
    is logged at WARNING with event=quarantine_resolution_miss and skipped — the
    sweep never raises."""
    sid = await seed_search_space(db_session)
    cid = await seed_concept(db_session, search_space_id=sid, subject_slug="s_or", concept_slug="c_or")
    # NO concept_problems row for 'ORPHAN'.
    sess = await _shared_session(db_session, search_space_id=sid, concept_id=cid)
    await _seed_attempts_with_findings(
        db_session, session_id=sess.id, search_space_id=sid, problem_code="ORPHAN",
        n_attempts=10, miss_plan=lambda i: ["bad"] if i < 9 else [],
    )

    with caplog.at_level(logging.WARNING, logger="apollo.provisioning.quarantine"):
        report = await sweep_quarantine(db_session, search_space_id=sid)

    assert report.n_quarantined == 0
    misses = [r for r in caplog.records
              if getattr(r, "event", None) == "quarantine_resolution_miss"]
    assert len(misses) == 1
    assert misses[0].problem_code == "ORPHAN"


async def test_sweep_clamps_multiple_findings_per_attempt_for_same_node(db_session, caplog):
    """miss_count (COUNT of missing_node findings) and N (COUNT-DISTINCT attempts)
    come from independent queries. If a single graded attempt carries >1
    missing_node finding for the SAME node key, raw miss_count can exceed N. The
    sweep CLAMPS miss_count to N (min) so the reconstructed coverage matrix stays
    well-formed (no ragged sequence) and every per-node miss rate stays a valid
    probability (<= 1.0).

    Here EACH of N=10 attempts carries TWO missing_node findings for 'dup'
    (raw count 20 > N=10), so 'dup' is the first key in the aggregation dict (the
    ``next(iter(...))`` source of N inside ``quarantine_decision``). The clamp
    pins 'dup' to exactly N=10 flags, so the audit log reports the TRUE
    ``n_attempts == 10``. The MUTATION (drop the min() clamp): 'dup' becomes a
    length-20 sequence, so ``quarantine_decision`` reads N=20 off it (the §review
    dict-order-dependent disagreement with true N) and the audit log reports
    ``n_attempts == 20``. The assertion ``n_attempts == 10`` REDs under the
    mutation."""
    sid = await seed_search_space(db_session)
    cid = await seed_concept(db_session, search_space_id=sid, subject_slug="s_cp", concept_slug="c_cp")
    p = await _seed_problem_row(db_session, concept_id=cid, search_space_id=sid, problem_code="P-DUP")
    sess = await _shared_session(db_session, search_space_id=sid, concept_id=cid)
    for i in range(10):
        attempt = ProblemAttempt(session_id=sess.id, problem_id="P-DUP", difficulty="intro")
        db_session.add(attempt)
        await db_session.flush()
        run = await _seed_run(db_session, attempt_id=attempt.id, search_space_id=sid)
        # TWO missing_node findings for the SAME node 'dup' in EVERY attempt, so
        # 'dup' is iterated FIRST (the dict-order N source) with a saturated count.
        db_session.add(GraphComparisonFinding(
            run_id=run.id, reference_node_ids=["dup"], finding_kind="missing_node"))
        db_session.add(GraphComparisonFinding(
            run_id=run.id, reference_node_ids=["dup"], finding_kind="missing_node"))
        if i == 0:
            db_session.add(GraphComparisonFinding(
                run_id=run.id, reference_node_ids=["low"], finding_kind="missing_node"))
    await db_session.flush()

    with caplog.at_level(logging.INFO, logger="apollo.provisioning.quarantine"):
        report = await sweep_quarantine(db_session, search_space_id=sid)

    assert report.n_quarantined == 1
    assert "P-DUP" in report.fired_problem_codes
    fires = [r for r in caplog.records if getattr(r, "event", None) == "quarantine_fire"]
    assert len(fires) == 1
    # The clamp pins N to the TRUE distinct-attempt count (10); unclamped, the
    # ragged length-20 'dup' sequence would make quarantine_decision read N=20.
    assert fires[0].n_attempts == 10
    assert fires[0].top_miss_rate == pytest.approx(1.0)
    refreshed = (await db_session.execute(
        select(ConceptProblem).where(ConceptProblem.id == p.id)
    )).scalar_one()
    assert refreshed.quarantined_at is not None


# ===========================================================================
# Layer 2 — the selector exclusion
# ===========================================================================


async def test_quarantined_tier2_problem_excluded_by_selector(db_session):
    """Two Tier-2 problems on one concept; one is quarantined (quarantined_at set
    directly). list_problems_for_concept returns ONLY the live problem.

    MUTATION-PROOF: removing the ``ConceptProblem.quarantined_at.is_(None)``
    predicate in list_problems_for_concept makes the quarantined problem
    selectable -> this test REDs. (Verified locally by neutering the predicate,
    observing the RED, then restoring.)"""
    payloads = load_bernoulli_problem_payloads()
    live_payload, quarantined_payload = payloads[0], payloads[1]
    sid = await seed_search_space(db_session)
    cid = await seed_concept(db_session, search_space_id=sid, subject_slug="s_sel", concept_slug="c_sel")

    from datetime import UTC, datetime
    db_session.add(ConceptProblem(
        concept_id=cid, problem_code=live_payload["id"], difficulty=live_payload["difficulty"],
        payload=live_payload, tier=2, search_space_id=sid,
    ))
    db_session.add(ConceptProblem(
        concept_id=cid, problem_code=quarantined_payload["id"],
        difficulty=quarantined_payload["difficulty"], payload=quarantined_payload,
        tier=2, search_space_id=sid, quarantined_at=datetime.now(UTC),
    ))
    await db_session.flush()

    problems = await list_problems_for_concept(db_session, concept_id=cid)
    returned_ids = {p.id for p in problems}
    assert live_payload["id"] in returned_ids
    assert quarantined_payload["id"] not in returned_ids, "quarantined problem must NOT be selectable"


async def test_live_tier2_problem_still_selectable(db_session):
    """Non-regression: a non-quarantined Tier-2 problem is still returned (the
    quarantine predicate did NOT over-filter)."""
    payload = load_bernoulli_problem_payloads()[0]
    sid = await seed_search_space(db_session)
    cid = await seed_concept(db_session, search_space_id=sid, subject_slug="s_lv", concept_slug="c_lv")
    db_session.add(ConceptProblem(
        concept_id=cid, problem_code=payload["id"], difficulty=payload["difficulty"],
        payload=payload, tier=2, search_space_id=sid,
    ))
    await db_session.flush()

    problems = await list_problems_for_concept(db_session, concept_id=cid)
    assert [p.id for p in problems] == [payload["id"]]


# ===========================================================================
# Layer 3 — THE shadow verification (§9 SPEC-4 — the spec-closing invariant)
# ===========================================================================
# Mirrors the proven test_done_shadow_route_postgres.py harness: the bernoulli
# intro ConceptProblem payload IS the faithful stand-in for an auto-provisioned
# Tier-2 problem (an auto-provisioned one is byte-identical in shape — it
# satisfies Problem + validate_reference_graph). The Neo4j boundary + LLM
# adjudicator/auditor are deterministic stubs; NO live API, NO real Neo4j.


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


async def _seed_shadow_session(db, *, current_code: str, sid: int, cid: int):
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
    db.add(Message(session_id=sess.id, attempt_id=attempt.id, role="student",
                   content="continuity says rho A1 v1 equals rho A2 v2", turn_index=0))
    await db.flush()
    return sess, attempt


def _adjudicator_stub(_request):
    return {"stu_continuity": "eq.continuity"}


def _auditor_stub(_request):
    return {"spans": {}}


def _neo_stubs(attempt_id: int):
    return [
        patch("apollo.handlers.done.KGStore.read_graph",
              new=AsyncMock(return_value=_student_graph(attempt_id))),
        patch("apollo.handlers.done.KGStore.freeze", new=AsyncMock()),
        patch("apollo.handlers.done.KGStore.stamp_graded_at", new=AsyncMock()),
        patch("apollo.handlers.done.KGStore.read_node_graded_at",
              new=AsyncMock(return_value={"stu_continuity": "2026-06-18T00:00:02+00:00"})),
        patch("apollo.handlers.done_grading.write_resolution",
              new=AsyncMock(return_value=None)),
        patch("apollo.handlers.done_turn_order.KGStore.read_node_created_at",
              new=AsyncMock(return_value={"stu_continuity": "2026-06-18T00:00:02+00:00"})),
        patch("apollo.handlers.done_grading.main_chat_adjudicator", new=_adjudicator_stub),
        patch("apollo.handlers.done_grading.main_chat_auditor", new=_auditor_stub),
    ]


async def _run_shadow_done(db, sess_id, monkeypatch, *, neo_patches):
    monkeypatch.setenv("APOLLO_GRAPH_SIM_SHADOW_ENABLED", "true")
    # LAYER3 OFF — explicit delenv so the gate cannot read a leaked "true".
    monkeypatch.delenv("APOLLO_GRAPH_SIM_LAYER3_ENABLED", raising=False)
    for p in neo_patches:
        p.start()
    try:
        return await handle_done(db=db, neo=object(), session_id=sess_id)
    finally:
        for p in reversed(neo_patches):
            p.stop()


async def test_shadow_on_layer3_off_teachable_produces_findings_zero_belief_movement(
    db_session, monkeypatch
):
    """§9 SPEC-4 — the spec-closing invariant. With SHADOW ON + LAYER3 OFF on an
    auto-provisioned-shaped Tier-2 problem, the FULL conjunction holds:

      (a) handle_done returns a teachable grade (rubric/coverage/xp_earned), and
          the attempt ends result='graded', learner_update_pending is False;
      (b) apollo_graph_comparison_findings rows exist for the attempt (>=1, and a
          missing_node finding) AND a GraphComparisonRun row exists -> quarantine
          HAS its data source;
      (c) apollo_mastery_events == 0 AND apollo_learner_state == 0 for the user ->
          ZERO Layer-3 belief movement.

    A failure of ANY part fails the test. This is the invariant that CLOSES the
    Apollo KG learner-model spec."""
    sid, cid, codes = await seed_course(
        db_session, subject_slug="fluid_mechanics",
        concept_slug="bernoulli_principle", problems=_INTRO,
    )
    sess, attempt = await _seed_shadow_session(db_session, current_code=codes[0], sid=sid, cid=cid)

    out = await _run_shadow_done(
        db_session, sess.id, monkeypatch, neo_patches=_neo_stubs(attempt.id)
    )

    # (a) teachable grade present + attempt committed graded, not pending
    assert "rubric" in out and "coverage" in out and "xp_earned" in out
    refreshed = (await db_session.execute(
        select(ProblemAttempt).where(ProblemAttempt.id == attempt.id)
    )).scalar_one()
    assert refreshed.result == "graded"
    assert refreshed.learner_update_pending is False

    # (b) the shadow run + findings exist (quarantine's data source)
    run_count = (await db_session.execute(
        select(func.count()).select_from(GraphComparisonRun)
        .where(GraphComparisonRun.attempt_id == attempt.id)
    )).scalar_one()
    assert run_count == 1
    finding_count = (await db_session.execute(
        select(func.count()).select_from(GraphComparisonFinding)
        .join(GraphComparisonRun, GraphComparisonFinding.run_id == GraphComparisonRun.id)
        .where(GraphComparisonRun.attempt_id == attempt.id)
    )).scalar_one()
    assert finding_count >= 1
    missing_count = (await db_session.execute(
        select(func.count()).select_from(GraphComparisonFinding)
        .join(GraphComparisonRun, GraphComparisonFinding.run_id == GraphComparisonRun.id)
        .where(GraphComparisonRun.attempt_id == attempt.id)
        .where(GraphComparisonFinding.finding_kind == "missing_node")
    )).scalar_one()
    assert missing_count >= 1

    # (c) ZERO Layer-3 belief movement (the verbatim me==0/ls==0 boundary)
    me = (await db_session.execute(
        select(func.count()).select_from(MasteryEvent)
        .where(MasteryEvent.user_id == TEST_USER_ID)
    )).scalar_one()
    ls = (await db_session.execute(
        select(func.count()).select_from(LearnerState)
        .where(LearnerState.user_id == TEST_USER_ID)
    )).scalar_one()
    assert me == 0
    assert ls == 0


async def test_shadow_findings_feed_sweep_end_to_end(db_session, monkeypatch):
    """THE BRIDGE (the two deliverables COMPOSE end-to-end).

    Runs the §6 SHADOW ``handle_done`` so the PRODUCTION grading core writes real
    ``missing_node`` findings, then runs ``sweep_quarantine`` over them. This is
    the test that proves the sweep's per-node aggregation actually consumes the
    field the production writer populates: a production ``missing_node`` finding
    carries ``entity_id = NULL`` and its node identity in ``reference_node_ids``
    (``apollo/grading/persistence.py`` finding_to_row_spec hardcodes
    ``entity_id=None`` in v1). The sweep MUST therefore key on
    ``reference_node_ids`` — if it keyed on ``entity_id`` (the original bug) the
    production findings would contribute ZERO node rows and quarantine could never
    fire end-to-end.

    Method: produce ONE real shadow finding, read its actual emitted
    ``reference_node_ids`` value, then top up to N_MIN graded attempts all missing
    that SAME node (concentrated) using the identical production-shaped key — and
    assert the sweep quarantines the problem. This documents the known limitation
    (the shadow path persists ``entity_id=NULL``) as a TESTED behavior: quarantine
    is functional against real grading output BECAUSE it keys on
    ``reference_node_ids``, not ``entity_id``."""
    sid, cid, codes = await seed_course(
        db_session, subject_slug="fluid_mechanics",
        concept_slug="bernoulli_principle", problems=_INTRO,
    )
    code = codes[0]
    # seed_course leaves ConceptProblem.search_space_id NULL; the sweep resolution
    # join is course-scoped (cp.search_space_id = runs.search_space_id), so stamp it.
    cp_row = (await db_session.execute(
        select(ConceptProblem).where(ConceptProblem.problem_code == code)
    )).scalar_one()
    cp_row.search_space_id = sid
    await db_session.flush()
    sess, attempt = await _seed_shadow_session(db_session, current_code=code, sid=sid, cid=cid)

    await _run_shadow_done(
        db_session, sess.id, monkeypatch, neo_patches=_neo_stubs(attempt.id)
    )

    # Read back a REAL production missing_node finding for this attempt.
    prod_finding = (await db_session.execute(
        select(GraphComparisonFinding)
        .join(GraphComparisonRun, GraphComparisonFinding.run_id == GraphComparisonRun.id)
        .where(GraphComparisonRun.attempt_id == attempt.id)
        .where(GraphComparisonFinding.finding_kind == "missing_node")
    )).scalars().first()
    assert prod_finding is not None
    # The production contract the sweep depends on: entity_id is NULL, the node
    # identity lives in a NON-EMPTY reference_node_ids list.
    assert prod_finding.entity_id is None
    assert prod_finding.reference_node_ids, "production missing_node finding must carry reference_node_ids"
    prod_node_key = list(prod_finding.reference_node_ids)

    # Top up to a concentrated N >= N_MIN: 9 more graded attempts all missing the
    # SAME production-shaped node (plus the 1 real shadow attempt == N=10, 10/10
    # concentrated on one node).
    await _seed_attempts_with_findings(
        db_session, session_id=sess.id, search_space_id=sid, problem_code=code,
        n_attempts=9, miss_plan=lambda i: [prod_node_key[0]],
    )

    report = await sweep_quarantine(db_session, search_space_id=sid)

    assert code in report.fired_problem_codes, (
        "shadow-produced findings (entity_id=NULL, keyed by reference_node_ids) "
        "must feed the sweep — quarantine fires end-to-end"
    )
    refreshed = (await db_session.execute(
        select(ConceptProblem).where(
            ConceptProblem.search_space_id == sid,
            ConceptProblem.problem_code == code,
        )
    )).scalar_one()
    assert refreshed.quarantined_at is not None
