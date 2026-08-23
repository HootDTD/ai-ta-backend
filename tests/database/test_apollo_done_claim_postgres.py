"""M1 (P3.4) — real-Postgres gates for the durable Done grading claim.

Memo's structural finding: `handle_done` spans 6-7 independent commits and takes
ZERO locks, while every sibling lifecycle handler row-locks the session for
exactly the double-click race Done is exposed to. No transaction-scoped
primitive can help — a `FOR UPDATE` taken at the first commit is released ~4
minutes before the last write — so serialization has to be a DURABLE
compare-and-swap claim.

Real Postgres is mandatory here: SQLite silently ignores row locking, and the
suite-wide `db_session` fixture cannot produce two connections that see each
other's commits.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select, update

from apollo.errors import GradingInProgressError
from apollo.handlers.done import (
    _STALE_CLAIM_AFTER,
    _claim_grading_slot,
    _fence_grade_commit,
    _release_grading_claim,
    _stored_grade_payload,
    handle_done,
)
from apollo.ontology import KGGraph
from apollo.persistence.models import (
    ProblemAttempt,
    SessionPhase,
    StudentProgress,
    TutoringMessage,
    TutoringSession,
)
from apollo.overseer.rubric import score_to_band
from apollo.persistence.progress_repo import apply_xp
from database.models import Course

pytestmark = pytest.mark.integration


# What `compute_rubric` is patched to return (the RAW axis overall) vs what the
# serving layer hands the student and persists in `served_overall`: the same
# score/letter plus the additive `band` (study-prep 2026-08-23). Written as one
# constant so the two can never drift apart in this file.
_B_MINUS_SERVED = {"score": 71, "letter": "B-", "band": score_to_band(71)}


async def _seed_session(maker, slug_prefix: str, *, phase: str | None) -> int:
    async with maker() as db:
        course = Course(name="P34 claim", slug=f"{slug_prefix}-claim", subject_name="Physics")
        db.add(course)
        await db.flush()
        sess = TutoringSession(user_id=str(uuid.uuid4()), search_space_id=course.id, phase=phase)
        db.add(sess)
        await db.commit()
        return int(sess.id)


async def _seed_graded_attempt(maker, slug_prefix: str) -> tuple[int, int, int, str, dict]:
    """Course -> session -> a `result="graded"` `ProblemAttempt` carrying a
    stored `diagnostic_report`. Returns
    `(course_id, session_id, attempt_id, user_id, report)`."""
    report = {
        "narrative": "Solid grasp of the conservation-of-momentum setup.",
        "rubric": {
            "overall": {"score": 60, "letter": "D"},
            "coverage_axis": {"score": 60},
        },
        "coverage": {"covered": ["node_1"], "missing": ["node_2"]},
        "served_overall": {"score": 82, "letter": "B"},
    }
    async with maker() as db:
        course = Course(name="P34 replay", slug=f"{slug_prefix}-replay", subject_name="Physics")
        db.add(course)
        await db.flush()
        user_id = str(uuid.uuid4())
        sess = TutoringSession(
            user_id=user_id, search_space_id=course.id, phase=SessionPhase.REPORT.value
        )
        db.add(sess)
        await db.flush()
        attempt = ProblemAttempt(
            session_id=sess.id,
            problem_id=9001,
            difficulty="intro",
            user_id=user_id,
            course_id=course.id,
            result="graded",
            diagnostic_report=report,
        )
        db.add(attempt)
        await db.commit()
        return int(course.id), int(sess.id), int(attempt.id), user_id, report


async def _phase(maker, session_id: int) -> str | None:
    async with maker() as db:
        return (
            await db.execute(select(TutoringSession.phase).where(TutoringSession.id == session_id))
        ).scalar_one()


async def test_exactly_one_of_two_concurrent_claims_wins(pg_committing_sessions):
    maker, slug_prefix = pg_committing_sessions
    session_id = await _seed_session(maker, slug_prefix, phase=SessionPhase.TEACHING.value)

    async def _claim() -> bool:
        async with maker() as db:
            return await _claim_grading_slot(db, session_id=session_id)

    outcomes = await asyncio.gather(_claim(), _claim())

    assert sorted(outcomes) == [False, True]
    assert await _phase(maker, session_id) == SessionPhase.SOLVING.value


async def test_claim_succeeds_on_a_null_phase(pg_committing_sessions):
    """`phase` is a NULLABLE Text column, and SQL `<>` never matches NULL — a
    plain `phase <> 'SOLVING'` predicate would refuse to claim a NULL-phase
    session forever. The predicate is `IS DISTINCT FROM`."""
    maker, slug_prefix = pg_committing_sessions
    session_id = await _seed_session(maker, slug_prefix, phase=None)

    async with maker() as db:
        assert await _claim_grading_slot(db, session_id=session_id) is True
    assert await _phase(maker, session_id) == SessionPhase.SOLVING.value


async def test_a_stale_claim_is_reclaimable(pg_committing_sessions):
    """A Done that crashed between the claim and the grade commit leaves the
    phase stuck at SOLVING forever and the attempt can never be re-graded. A
    claim whose session row has not been touched for `_STALE_CLAIM_AFTER` is
    reclaimable."""
    maker, slug_prefix = pg_committing_sessions
    session_id = await _seed_session(maker, slug_prefix, phase=SessionPhase.SOLVING.value)

    async with maker() as db:
        assert await _claim_grading_slot(db, session_id=session_id) is False

        stale = datetime.now(UTC) - _STALE_CLAIM_AFTER - timedelta(minutes=1)
        await db.execute(
            update(TutoringSession)
            .where(TutoringSession.id == session_id)
            .values(updated_at=stale)
            .execution_options(synchronize_session=False)
        )
        await db.commit()

        assert await _claim_grading_slot(db, session_id=session_id) is True


async def test_release_restores_the_prior_phase(pg_committing_sessions):
    maker, slug_prefix = pg_committing_sessions
    session_id = await _seed_session(maker, slug_prefix, phase=SessionPhase.TEACHING.value)

    async with maker() as db:
        assert await _claim_grading_slot(db, session_id=session_id) is True
        await _release_grading_claim(
            db, session_id=session_id, prior_phase=SessionPhase.TEACHING.value
        )

    assert await _phase(maker, session_id) == SessionPhase.TEACHING.value
    async with maker() as db:
        assert await _claim_grading_slot(db, session_id=session_id) is True


async def test_release_of_a_reclaimed_stale_claim_falls_back_to_teaching(
    pg_committing_sessions,
):
    """When the claim was a RECLAIM, `prior_phase` is itself 'SOLVING' —
    restoring it verbatim would leave the attempt bricked. Fall back to
    TEACHING (the same phase `handle_retry` resets to)."""
    maker, slug_prefix = pg_committing_sessions
    session_id = await _seed_session(maker, slug_prefix, phase=SessionPhase.SOLVING.value)

    async with maker() as db:
        await _release_grading_claim(
            db, session_id=session_id, prior_phase=SessionPhase.SOLVING.value
        )

    assert await _phase(maker, session_id) == SessionPhase.TEACHING.value


async def test_release_never_clobbers_a_later_claim(pg_committing_sessions):
    """The release is itself a CAS guarded on `phase = 'SOLVING'`, so a release
    arriving after the session already moved on (REPORT) is a no-op.

    P3.4 fix-round-2 note: this is phase-only, deliberately (a fencing-token
    guard was tried and reverted — see `_release_grading_claim`'s docstring).
    The ACCEPTED residual this leaves is the mirror image of this test: a
    release racing a LIVE reclaim that is STILL sitting at `phase = 'SOLVING'`
    (not yet REPORT) is NOT distinguishable from this test's REPORT case and
    DOES clobber it — see the docstring for why that's an availability cost,
    never a grade-integrity one, and judged acceptable versus the token's
    common-case regression."""
    maker, slug_prefix = pg_committing_sessions
    session_id = await _seed_session(maker, slug_prefix, phase=SessionPhase.REPORT.value)

    async with maker() as db:
        await _release_grading_claim(
            db, session_id=session_id, prior_phase=SessionPhase.TEACHING.value
        )

    assert await _phase(maker, session_id) == SessionPhase.REPORT.value


async def test_stored_grade_payload_replays_without_side_effects(pg_committing_sessions):
    """A double-clicked Done on an already-graded attempt must be shown the
    persisted grade verbatim, award zero XP, and mutate NOTHING — not the
    student's progress row, not the attempt's `diagnostic_report`.
    `_stored_grade_payload` must NOT use `progress_repo.load_progress` (it
    upserts + commits); this asserts that contract on real Postgres rather
    than in a mock."""
    maker, slug_prefix = pg_committing_sessions
    course_id, session_id, attempt_id, user_id, report = await _seed_graded_attempt(
        maker, slug_prefix
    )

    # A real prior XP award, so a mutation would be observable as a changed total.
    async with maker() as db:
        await apply_xp(db=db, user_id=user_id, course_id=course_id, xp_delta=50)

    async with maker() as db:
        sess = (
            await db.execute(select(TutoringSession).where(TutoringSession.id == session_id))
        ).scalar_one()
        attempt = (
            await db.execute(select(ProblemAttempt).where(ProblemAttempt.id == attempt_id))
        ).scalar_one()
        result = await _stored_grade_payload(db, sess=sess, attempt=attempt)

    assert result is not None
    # The replay serves the snapshot verbatim PLUS the additive band. This row is
    # seeded WITHOUT one (a pre-2026-08-23 grade), so the band is derived from the
    # snapshot's own score — the fallback half of `band_from_served_overall`'s
    # snapshot-first rule, exercised here against real Postgres JSONB.
    assert result["rubric"]["overall"] == {
        **report["served_overall"],
        "band": score_to_band(report["served_overall"]["score"]),
    }
    assert result["diagnostic_narrative"] == report["narrative"]
    assert result["coverage"] == report["coverage"]
    assert result["already_graded"] is True
    assert result["xp_earned"] == 0
    assert result["xp_before"] == 50
    assert result["xp_after"] == 50

    async with maker() as db:
        progress = (
            await db.execute(
                select(StudentProgress).where(
                    StudentProgress.user_id == user_id,
                    StudentProgress.course_id == course_id,
                )
            )
        ).scalar_one()
        assert int(progress.xp_total) == 50

        attempt_after = (
            await db.execute(select(ProblemAttempt).where(ProblemAttempt.id == attempt_id))
        ).scalar_one()
        assert attempt_after.diagnostic_report == report
        assert attempt_after.result == "graded"


# ---------------------------------------------------------------------------
# End-to-end: two REAL concurrent handle_done calls on independent connections.
# ---------------------------------------------------------------------------

_PROBLEM_ID = 4242


def _fake_problem() -> MagicMock:
    problem = MagicMock()
    problem.id = "p_code"
    problem.database_id = _PROBLEM_ID
    problem.problem_text = "text"
    problem.concept_id = "bernoulli_principle"
    problem.reference_solution = []
    problem.to_kg_graph.return_value = KGGraph()
    return problem


def _grading_patches() -> list:
    """Deterministic collaborators so no LLM / Neo4j call runs. `apply_xp` is
    deliberately REAL — the double-award is the thing under test."""
    return [
        patch("apollo.handlers.done._find_problem", new=AsyncMock(return_value=_fake_problem())),
        patch("apollo.handlers.done.KGStore.read_graph", new=AsyncMock(return_value=KGGraph())),
        patch("apollo.handlers.done.KGStore.stamp_graded_at", new=AsyncMock()),
        patch(
            "apollo.handlers.done.compute_transcript_coverage_with_spans",
            new=AsyncMock(return_value=({}, {})),
        ),
        patch("apollo.handlers.done.compute_topic_score", new=MagicMock(return_value=None)),
        patch(
            "apollo.handlers.done.compute_rubric",
            return_value={"overall": {"score": 71, "letter": "B-"}},
        ),
        patch("apollo.handlers.done.generate_diagnostic", return_value="narrative"),
        patch("apollo.handlers.done.compute_xp_earned", return_value=10),
        patch("apollo.handlers.done.write_artifacts", new=AsyncMock(return_value=None)),
    ]


async def _seed_gradable_attempt(maker, slug_prefix: str) -> tuple[int, int, str, int]:
    """Course -> session (TEACHING) -> attempt -> one student message.
    Returns (session_id, attempt_id, user_id, course_id)."""
    async with maker() as db:
        course = Course(name="P34 done race", slug=f"{slug_prefix}-done", subject_name="Physics")
        db.add(course)
        await db.flush()
        sess = TutoringSession(
            user_id=str(uuid.uuid4()),
            search_space_id=course.id,
            concept_id=None,
            phase=SessionPhase.TEACHING.value,
            current_problem_id=_PROBLEM_ID,
        )
        db.add(sess)
        await db.flush()
        attempt = ProblemAttempt(
            session_id=sess.id,
            problem_id=_PROBLEM_ID,
            difficulty="intro",
            user_id=sess.user_id,
            course_id=course.id,
        )
        db.add(attempt)
        await db.flush()
        db.add(
            TutoringMessage(
                session_id=sess.id,
                course_id=course.id,
                attempt_id=attempt.id,
                role="student",
                content="Continuity says rho A1 v1 equals rho A2 v2.",
                turn_index=0,
            )
        )
        await db.commit()
        return int(sess.id), int(attempt.id), str(sess.user_id), int(course.id)


async def test_double_done_grades_once_and_awards_xp_once(pg_committing_sessions):
    """Memo §1: both Dones used to read `attempt.result is None`, both computed
    `is_reattempt=False`, and both called `apply_xp` with undiscounted XP — and
    both wrote `diagnostic_report`, so the student could be SHOWN a grade that
    is not the one persisted. Only the artifact's unique key stopped the loser,
    by which point phase, grade and XP were already durable."""
    maker, slug_prefix = pg_committing_sessions
    session_id, attempt_id, user_id, course_id = await _seed_gradable_attempt(maker, slug_prefix)

    async def _done():
        async with maker() as db:
            try:
                return await handle_done(db=db, neo=object(), session_id=session_id)
            except GradingInProgressError as exc:
                return exc

    patches = _grading_patches()
    for p in patches:
        p.start()
    try:
        first, second = await asyncio.gather(_done(), _done())
    finally:
        for p in reversed(patches):
            p.stop()

    outcomes = [first, second]
    graded = [o for o in outcomes if isinstance(o, dict)]
    refused = [o for o in outcomes if isinstance(o, GradingInProgressError)]
    assert len(graded) == 1
    assert len(refused) == 1
    assert graded[0]["xp_earned"] == 10

    async with maker() as db:
        progress = (
            await db.execute(
                select(StudentProgress).where(
                    StudentProgress.user_id == user_id,
                    StudentProgress.course_id == course_id,
                )
            )
        ).scalar_one()
        assert int(progress.xp_total) == 10

        attempt = (
            await db.execute(select(ProblemAttempt).where(ProblemAttempt.id == attempt_id))
        ).scalar_one()
        assert attempt.result == "graded"
        assert attempt.diagnostic_report["served_overall"] == _B_MINUS_SERVED
    assert await _phase(maker, session_id) == SessionPhase.REPORT.value


async def test_a_second_done_after_the_first_finishes_replays_the_stored_grade(
    pg_committing_sessions,
):
    """Sequential double-click: the claim is free (phase is REPORT), but the
    attempt is already graded, so the short-circuit serves the persisted report
    with zero XP instead of re-adjudicating it."""
    maker, slug_prefix = pg_committing_sessions
    session_id, attempt_id, user_id, course_id = await _seed_gradable_attempt(maker, slug_prefix)

    patches = _grading_patches()
    for p in patches:
        p.start()
    try:
        async with maker() as db:
            first = await handle_done(db=db, neo=object(), session_id=session_id)
        async with maker() as db:
            second = await handle_done(db=db, neo=object(), session_id=session_id)
    finally:
        for p in reversed(patches):
            p.stop()

    assert first["xp_earned"] == 10
    assert "already_graded" not in first
    assert second["already_graded"] is True
    assert second["xp_earned"] == 0
    assert second["rubric"]["overall"] == _B_MINUS_SERVED

    async with maker() as db:
        progress = (
            await db.execute(
                select(StudentProgress).where(
                    StudentProgress.user_id == user_id,
                    StudentProgress.course_id == course_id,
                )
            )
        ).scalar_one()
        assert int(progress.xp_total) == 10


# ---------------------------------------------------------------------------
# M1b delta: the terminal phase transition is itself fenced.
# ---------------------------------------------------------------------------


async def test_terminal_fence_rejects_a_done_reclaimed_out_from_under_it(
    pg_committing_sessions,
):
    """A Done (A) claims the slot and then stalls past `_STALE_CLAIM_AFTER`
    (crash/hang, not a normal double-click). A second Done (B) is entitled to
    reclaim it — and does, then runs a full (patched) Done to completion. If A
    eventually reaches its OWN terminal write anyway (a zombie that never
    crashed, just hung), `_fence_grade_commit` must refuse it: the guard is
    `phase = _CLAIM_PHASE` ('SOLVING'), and B has already moved the phase to
    REPORT, so rowcount is 0.

    This has to call `_fence_grade_commit` directly rather than driving a
    second `handle_done` call for A: a FRESH `handle_done` call made after B
    finishes would hit the already-graded short-circuit and never reach the
    claim/fence code at all — proving the fence only matters for a Done that
    was ALREADY mid-pipeline when it lost the race, exactly what this test
    reconstructs. The routing contract that a lost fence RAISES
    `GradingInProgressError` and writes nothing is covered on mocks in
    `apollo/handlers/tests/test_done_claim_unit.py::test_fenced_out_mid_grade_raises_and_writes_nothing`."""
    maker, slug_prefix = pg_committing_sessions
    session_id, attempt_id, user_id, course_id = await _seed_gradable_attempt(maker, slug_prefix)

    # A claims the slot.
    async with maker() as db:
        assert await _claim_grading_slot(db, session_id=session_id) is True

    # A stalls: age its claim past the 15-minute staleness window so B's
    # reclaim below is a LEGITIMATE reclaim, not a bug.
    async with maker() as db:
        await db.execute(
            update(TutoringSession)
            .where(TutoringSession.id == session_id)
            .values(updated_at=datetime.now(UTC) - _STALE_CLAIM_AFTER - timedelta(minutes=1))
            .execution_options(synchronize_session=False)
        )
        await db.commit()

    # B reclaims and runs a full (patched) Done to completion — the grade of
    # record.
    patches = _grading_patches()
    for p in patches:
        p.start()
    try:
        async with maker() as db:
            b_result = await handle_done(db=db, neo=object(), session_id=session_id)
    finally:
        for p in reversed(patches):
            p.stop()
    assert b_result["xp_earned"] == 10
    assert await _phase(maker, session_id) == SessionPhase.REPORT.value

    # A finally reaches ITS terminal fence — too late, B already owns REPORT.
    async with maker() as db:
        fenced_in = await _fence_grade_commit(db, session_id=session_id)
        await db.rollback()
    assert fenced_in is False

    # B's grade of record — attempt, XP, phase — is untouched by A's attempt.
    async with maker() as db:
        attempt = (
            await db.execute(select(ProblemAttempt).where(ProblemAttempt.id == attempt_id))
        ).scalar_one()
        assert attempt.result == "graded"
        assert attempt.diagnostic_report["served_overall"] == _B_MINUS_SERVED

        progress = (
            await db.execute(
                select(StudentProgress).where(
                    StudentProgress.user_id == user_id,
                    StudentProgress.course_id == course_id,
                )
            )
        ).scalar_one()
        assert int(progress.xp_total) == 10
    assert await _phase(maker, session_id) == SessionPhase.REPORT.value


# ---------------------------------------------------------------------------
# Fix-round-1 CRITICAL: the post-claim already-graded re-check.
# ---------------------------------------------------------------------------


async def test_post_claim_recheck_serves_the_stored_grade_after_a_stale_hoist(
    pg_committing_sessions,
):
    """The already-graded hoist reads `attempt` BEFORE the claim, and the
    claim CAS inspects only the SESSION row's phase. A Done (X) that stalls
    in `store.read_graph` while ANOTHER Done (Y) grades the SAME attempt to
    completion wakes to find `phase='REPORT'` — which the claim predicate
    treats as freely claimable (`IS DISTINCT FROM 'SOLVING'`) — and
    legitimately WINS the claim on an attempt its own stale, pre-claim
    snapshot still says is ungraded. Without the post-claim re-check, X would
    fall through to `_grade_claimed_attempt`, re-grade, overwrite
    `diagnostic_report`, and double-award XP.

    Reproduced with REAL concurrency rather than mocked internals: X's
    `read_graph` blocks on an `asyncio.Event` that only gets set once the
    OTHER concurrent `handle_done` call has fully committed its grade — so
    whichever of the two calls reaches `read_graph` first is, by
    construction, the one whose claim lands AFTER a real grade of record
    already exists. Two SEPARATE sequential `handle_done` calls could not
    exercise this: a second call made strictly after the first commits would
    hit the top-level already-graded short-circuit and never even attempt the
    claim."""
    maker, slug_prefix = pg_committing_sessions
    session_id, attempt_id, user_id, course_id = await _seed_gradable_attempt(maker, slug_prefix)

    grader_finished = asyncio.Event()
    call_count = {"n": 0}

    async def _read_graph_side_effect(*, attempt_id):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # The FIRST Done to reach read_graph stalls here until the SECOND
            # has fully graded — reproducing "X stalls while Y grades to
            # completion". Whichever coroutine gets here first becomes X by
            # construction; the other, unblocked, becomes Y.
            await grader_finished.wait()
        return KGGraph()

    patches = [p for p in _grading_patches() if getattr(p, "attribute", None) != "read_graph"] + [
        patch(
            "apollo.handlers.done.KGStore.read_graph",
            new=AsyncMock(side_effect=_read_graph_side_effect),
        ),
    ]

    async def _done():
        async with maker() as db:
            try:
                result = await handle_done(db=db, neo=object(), session_id=session_id)
            except Exception as exc:  # noqa: BLE001 — surfaced via assertions below
                result = exc
        # Only the unblocked (Y) coroutine reaches this promptly; the
        # blocked (X) coroutine is stuck inside `read_graph` until Y's
        # `.set()` call below wakes it. Idempotent either way.
        grader_finished.set()
        return result

    for p in patches:
        p.start()
    try:
        first, second = await asyncio.gather(_done(), _done())
    finally:
        for p in reversed(patches):
            p.stop()

    outcomes = [first, second]
    assert not any(isinstance(o, Exception) for o in outcomes), outcomes

    graded_fresh = [o for o in outcomes if "already_graded" not in o]
    served_stored = [o for o in outcomes if o.get("already_graded") is True]
    assert len(graded_fresh) == 1
    assert len(served_stored) == 1
    assert graded_fresh[0]["xp_earned"] == 10
    assert served_stored[0]["xp_earned"] == 0
    assert served_stored[0]["rubric"]["overall"] == _B_MINUS_SERVED

    # XP was NOT double-awarded, and the session lands back on REPORT — the
    # accidental claim-winner's release restored the TRUE phase (REPORT, the
    # grade of record), not TEACHING.
    async with maker() as db:
        progress = (
            await db.execute(
                select(StudentProgress).where(
                    StudentProgress.user_id == user_id,
                    StudentProgress.course_id == course_id,
                )
            )
        ).scalar_one()
        assert int(progress.xp_total) == 10

        attempt = (
            await db.execute(select(ProblemAttempt).where(ProblemAttempt.id == attempt_id))
        ).scalar_one()
        assert attempt.result == "graded"
    assert await _phase(maker, session_id) == SessionPhase.REPORT.value
