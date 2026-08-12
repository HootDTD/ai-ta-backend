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

import pytest
from sqlalchemy import select, update

from apollo.handlers.done import (
    _STALE_CLAIM_AFTER,
    _claim_grading_slot,
    _release_grading_claim,
    _stored_grade_payload,
)
from apollo.persistence.models import (
    ProblemAttempt,
    SessionPhase,
    StudentProgress,
    TutoringSession,
)
from apollo.persistence.progress_repo import apply_xp
from database.models import Course

pytestmark = pytest.mark.integration


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
    arriving after the session already moved on (REPORT) is a no-op."""
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
    assert result["rubric"]["overall"] == report["served_overall"]
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
