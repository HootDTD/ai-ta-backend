"""M4 (P3.4) — a teaching turn must not write into an in-flight grading window.

Memo §3: nothing stopped a whole teaching turn from committing during the
~4-minute grading window. `handle_chat` had NO phase check at all (repo grep:
zero hits for `phase`/`frozen`); the only fresh-state check in the turn was
`KGStore._ensure_unfrozen`, which guards Neo4j writes ONLY. So during grading,
KG writes 409'd but transcript messages and ledger rows still committed — and
`handle_done` reads the transcript and the ledger as single unlocked SELECTs, so
whatever lands after those instants is silently orphaned.

Cross-connection by construction (one connection holds the claim, another runs
the turn), so this belongs on real Postgres.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from apollo.errors import SessionFrozenError
from apollo.handlers.chat import handle_chat
from apollo.persistence.models import (
    ProblemAttempt,
    SessionPhase,
    TutoringMessage,
    TutoringSession,
)
from database.models import Course

pytestmark = pytest.mark.integration

_PROBLEM_ID = 4242


async def _seed(maker, slug_prefix: str, *, phase: str) -> tuple[int, int]:
    async with maker() as db:
        course = Course(name="P34 chat guard", slug=f"{slug_prefix}-chat", subject_name="Physics")
        db.add(course)
        await db.flush()
        sess = TutoringSession(
            user_id=str(uuid.uuid4()),
            search_space_id=course.id,
            phase=phase,
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
        await db.commit()
        return int(sess.id), int(attempt.id)


async def test_chat_refuses_a_turn_while_a_done_holds_the_claim(pg_committing_sessions):
    """No patches needed: the guard fires before ANY collaborator (concept load,
    aside lane, intent classify), which is exactly the point — the turn is
    refused BEFORE the LLM spend, not half-written after it."""
    maker, slug_prefix = pg_committing_sessions
    session_id, attempt_id = await _seed(
        maker, slug_prefix, phase=SessionPhase.SOLVING.value
    )

    async with maker() as db:
        with pytest.raises(SessionFrozenError):
            await handle_chat(
                db=db, neo=None, session_id=session_id, message="one more thing"
            )

    async with maker() as db:
        count = (
            await db.execute(
                select(func.count())
                .select_from(TutoringMessage)
                .where(TutoringMessage.attempt_id == attempt_id)
            )
        ).scalar_one()
        assert count == 0
