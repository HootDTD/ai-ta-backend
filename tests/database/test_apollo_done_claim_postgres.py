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
)
from apollo.persistence.models import SessionPhase, TutoringSession
from database.models import Course
from tests.database._concurrency_fixtures import pg_committing_sessions  # noqa: F401

pytestmark = pytest.mark.integration


async def _seed_session(maker, slug_prefix: str, *, phase: str | None) -> int:
    async with maker() as db:
        course = Course(name="P34 claim", slug=f"{slug_prefix}-claim", subject_name="Physics")
        db.add(course)
        await db.flush()
        sess = TutoringSession(
            user_id=str(uuid.uuid4()), search_space_id=course.id, phase=phase
        )
        db.add(sess)
        await db.commit()
        return int(sess.id)


async def _phase(maker, session_id: int) -> str | None:
    async with maker() as db:
        return (
            await db.execute(
                select(TutoringSession.phase).where(TutoringSession.id == session_id)
            )
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
