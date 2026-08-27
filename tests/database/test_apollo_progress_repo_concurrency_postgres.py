"""M2 (P3.4) — real-Postgres concurrency gates for the XP repository.

Memo §7, the sharpest gap: ``apply_xp`` was a textbook lost update
(``xp_after = xp_before + xp_delta``; no ``FOR UPDATE``, no
``SET xp_total = xp_total + :delta``, no version column) and ``load_progress``
was check-then-insert with no ``ON CONFLICT``. Two concurrent Dones therefore
either lost an award outright or 500'd on the composite-PK ``IntegrityError``
AFTER the grade had already committed.

These MUST run on real Postgres: the lost update needs two genuinely concurrent
connections, which the rollback-scoped ``db_session`` fixture cannot provide.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

from apollo.persistence.models import StudentProgress
from apollo.persistence.progress_repo import apply_xp, load_progress
from database.models import Course

pytestmark = pytest.mark.integration


async def _seed_course(maker, slug_prefix: str) -> int:
    async with maker() as db:
        course = Course(name="P34 XP race", slug=f"{slug_prefix}-xp", subject_name="Physics")
        db.add(course)
        await db.commit()
        return int(course.id)


async def test_concurrent_apply_xp_awards_are_not_lost(pg_committing_sessions):
    """Two concurrent Dones for the same student must sum, not clobber.

    Pre-M2 this returned 10: both connections read xp_total=0 and both wrote 10.
    """
    maker, slug_prefix = pg_committing_sessions
    course_id = await _seed_course(maker, slug_prefix)
    user_id = str(uuid.uuid4())

    async def _award() -> int:
        async with maker() as db:
            result = await apply_xp(db=db, user_id=user_id, course_id=course_id, xp_delta=10)
            return int(result["xp_after"])

    await asyncio.gather(_award(), _award())

    async with maker() as db:
        row = (
            await db.execute(
                select(StudentProgress).where(
                    StudentProgress.user_id == user_id,
                    StudentProgress.course_id == course_id,
                )
            )
        ).scalar_one()
        assert int(row.xp_total) == 20


async def test_concurrent_first_award_does_not_raise_integrity_error(pg_committing_sessions):
    """The student's FIRST graded attempt in a course, raced.

    Pre-M2 the loser's check-then-insert hit the composite PK and raised
    ``IntegrityError`` out of ``apply_xp`` — a 500 on a request whose grade had
    already committed one step earlier.
    """
    maker, slug_prefix = pg_committing_sessions
    course_id = await _seed_course(maker, slug_prefix)
    user_id = str(uuid.uuid4())

    async def _load() -> int:
        async with maker() as db:
            row = await load_progress(db=db, user_id=user_id, course_id=course_id)
            return int(row.xp_total)

    totals = await asyncio.gather(_load(), _load())
    assert totals == [0, 0]

    async with maker() as db:
        rows = (
            (
                await db.execute(
                    select(StudentProgress).where(
                        StudentProgress.user_id == user_id,
                        StudentProgress.course_id == course_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1


async def test_level_ratchets_up_from_the_atomic_total(pg_committing_sessions):
    """The level written back is derived from the RETURNING total, and the
    guarded write (``WHERE level < :level_after``) means a slower racer can
    never ratchet the level back DOWN."""
    maker, slug_prefix = pg_committing_sessions
    course_id = await _seed_course(maker, slug_prefix)
    user_id = str(uuid.uuid4())

    async def _award(delta: int) -> None:
        async with maker() as db:
            await apply_xp(db=db, user_id=user_id, course_id=course_id, xp_delta=delta)

    await asyncio.gather(_award(200), _award(200))

    async with maker() as db:
        row = (
            await db.execute(
                select(StudentProgress).where(
                    StudentProgress.user_id == user_id,
                    StudentProgress.course_id == course_id,
                )
            )
        ).scalar_one()
        assert int(row.xp_total) == 400
        assert int(row.level) >= 2
        assert row.last_level_up_at is not None
