"""M5 (P3.4) — real-Postgres gate for the atomic ledger increment.

Memo §6: `controller.py`'s `target_row.times_asked = int(target_row.times_asked) + 1`
was a Python read-modify-write with no lock and no version column. Two
overlapping turns of the same attempt (a double-send) both read N and both wrote
N+1, losing an increment. That is GRADE-VISIBLE, not cosmetic: `done`'s
`_probed_node_ids` treats `times_asked > 0` as engagement, so a lost increment
mis-scopes the P1.2b denominator, and the per-node ask cap stops binding.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

from apollo.persistence.models import ProblemAttempt, QuestionOpportunity, TutoringSession
from apollo.smart_questions.controller import _bump_times_asked
from database.models import Course

pytestmark = pytest.mark.integration


async def _seed_ledger_row(maker, slug_prefix: str) -> tuple[int, int]:
    """Course -> session -> attempt -> one QuestionOpportunity row. Returns
    (course_id, opportunity_id)."""
    async with maker() as db:
        course = Course(name="P34 ledger race", slug=f"{slug_prefix}-qo", subject_name="Physics")
        db.add(course)
        await db.flush()
        sess = TutoringSession(user_id=str(uuid.uuid4()), search_space_id=course.id)
        db.add(sess)
        await db.flush()
        attempt = ProblemAttempt(
            session_id=sess.id,
            problem_id=4242,
            difficulty="intro",
            user_id=sess.user_id,
            course_id=course.id,
        )
        db.add(attempt)
        await db.flush()
        row = QuestionOpportunity(
            course_id=course.id,
            session_id=sess.id,
            attempt_id=attempt.id,
            reference_node_id="ref_node_1",
            state="missing",
            question="",
            evidence=[],
            times_asked=0,
        )
        db.add(row)
        await db.commit()
        return int(course.id), int(row.id)


async def test_concurrent_probe_increments_are_not_lost(pg_committing_sessions):
    """Pre-M5 this landed on 1: both connections read 0 and both wrote 1."""
    maker, slug_prefix = pg_committing_sessions
    _course_id, opportunity_id = await _seed_ledger_row(maker, slug_prefix)

    async def _bump() -> None:
        async with maker() as db:
            row = (
                await db.execute(
                    select(QuestionOpportunity).where(QuestionOpportunity.id == opportunity_id)
                )
            ).scalar_one()
            await _bump_times_asked(db, row=row)
            await db.commit()

    await asyncio.gather(_bump(), _bump())

    async with maker() as db:
        row = (
            await db.execute(
                select(QuestionOpportunity).where(QuestionOpportunity.id == opportunity_id)
            )
        ).scalar_one()
        assert int(row.times_asked) == 2


async def test_bump_does_not_re_emit_a_blind_write_at_commit(pg_committing_sessions):
    """The new value is set with `set_committed_value`, so the ORM does NOT
    consider `times_asked` dirty. If it did, the later flush would emit a blind
    `SET times_asked = <our value>` and reintroduce the lost update this
    replaces."""
    maker, slug_prefix = pg_committing_sessions
    _course_id, opportunity_id = await _seed_ledger_row(maker, slug_prefix)

    async with maker() as db:
        row = (
            await db.execute(
                select(QuestionOpportunity).where(QuestionOpportunity.id == opportunity_id)
            )
        ).scalar_one()
        await _bump_times_asked(db, row=row)
        assert int(row.times_asked) == 1
        assert row not in db.dirty

        # A concurrent bump lands between our bump and our commit. `db`'s own
        # atomic UPDATE above already holds Postgres's row lock (uncommitted),
        # so `other`'s UPDATE physically cannot complete until `db` commits and
        # releases it — run it as a background task instead of inline so it can
        # sit blocked on that lock without deadlocking this coroutine, the way
        # a real second in-flight request would.
        async def _concurrent_bump() -> None:
            async with maker() as other:
                other_row = (
                    await other.execute(
                        select(QuestionOpportunity).where(QuestionOpportunity.id == opportunity_id)
                    )
                ).scalar_one()
                await _bump_times_asked(other, row=other_row)
                await other.commit()

        concurrent = asyncio.create_task(_concurrent_bump())
        await asyncio.sleep(0)  # yield so the task reaches its blocked UPDATE
        await db.commit()
        await concurrent

    async with maker() as db:
        row = (
            await db.execute(
                select(QuestionOpportunity).where(QuestionOpportunity.id == opportunity_id)
            )
        ).scalar_one()
        assert int(row.times_asked) == 2
