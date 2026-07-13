from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.conftest import TEST_SPACE_ID, TEST_USER_ID
from apollo.handlers.lifecycle import handle_retry
from apollo.persistence.models import (
    ApolloSession,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
)
from database.models import Base


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn,
                tables=[ApolloSession.__table__, ProblemAttempt.__table__],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _seed(db: AsyncSession, *, result: str | None):
    session = ApolloSession(
        user_id=TEST_USER_ID,
        search_space_id=TEST_SPACE_ID,
        concept_id=1,
        status=SessionStatus.active.value,
        phase=SessionPhase.REPORT.value,
        current_problem_id="p1",
        pending_intent="done",
        history_summary="old attempt summary",
        history_summary_up_to_turn=12,
    )
    db.add(session)
    await db.flush()
    attempt = ProblemAttempt(
        session_id=session.id,
        problem_id="p1",
        difficulty="intro",
        result=result,
    )
    db.add(attempt)
    await db.commit()
    return session, attempt


@pytest.mark.asyncio
async def test_retry_after_grade_creates_blank_attempt_and_clears_context(db):
    session, old_attempt = await _seed(db, result="solved")

    response = await handle_retry(db=db, session_id=session.id)

    attempts = (
        (
            await db.execute(
                select(ProblemAttempt)
                .where(ProblemAttempt.session_id == session.id)
                .order_by(ProblemAttempt.id)
            )
        )
        .scalars()
        .all()
    )
    assert [attempt.id for attempt in attempts] == [old_attempt.id, response["attempt_id"]]
    assert attempts[-1].result is None
    assert attempts[-1].problem_id == old_attempt.problem_id
    assert attempts[-1].difficulty == old_attempt.difficulty
    assert session.phase == SessionPhase.TEACHING.value
    assert session.pending_intent is None
    assert session.history_summary is None
    assert session.history_summary_up_to_turn is None


@pytest.mark.asyncio
async def test_retry_in_flight_does_not_orphan_attempt_but_clears_context(db):
    session, attempt = await _seed(db, result=None)

    response = await handle_retry(db=db, session_id=session.id)

    count = await db.scalar(
        select(func.count(ProblemAttempt.id)).where(ProblemAttempt.session_id == session.id)
    )
    assert count == 1
    assert response == {"ok": True, "attempt_id": None}
    assert attempt.result is None
    assert session.phase == SessionPhase.TEACHING.value
    assert session.pending_intent is None
    assert session.history_summary is None
    assert session.history_summary_up_to_turn is None
