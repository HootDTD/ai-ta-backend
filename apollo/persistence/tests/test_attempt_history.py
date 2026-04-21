from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.persistence.attempt_history import has_prior_graded_attempt
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
        await conn.run_sync(lambda sc: Base.metadata.create_all(
            sc, tables=[ApolloSession.__table__, ProblemAttempt.__table__]
        ))
        # The unique-active-per-student index on ApolloSession is defined
        # as a Postgres partial index (postgresql_where=status='active').
        # SQLite ignores the WHERE clause and treats it as a full unique
        # index, which breaks legitimate test setups that create multiple
        # sessions per student. Drop the index here so SQLite behavior
        # matches Postgres semantics for this test.
        await conn.execute(text("DROP INDEX IF EXISTS ix_apollo_sessions_unique_active_per_student"))
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        yield s
    await engine.dispose()


async def _mk_session(db: AsyncSession, student_id: str) -> ApolloSession:
    s = ApolloSession(
        student_id=student_id,
        concept_cluster_id="fluid_mechanics",
        status=SessionStatus.active.value,
        phase=SessionPhase.TEACHING.value,
        current_problem_id="p1",
    )
    db.add(s)
    await db.flush()
    return s


async def _mk_attempt(db: AsyncSession, *, session_id: int, problem_id: str,
                     result: str | None) -> ProblemAttempt:
    a = ProblemAttempt(
        session_id=session_id,
        problem_id=problem_id,
        difficulty="intro",
        result=result,
    )
    db.add(a)
    await db.flush()
    return a


@pytest.mark.asyncio
async def test_returns_false_when_no_prior_attempts(db):
    sess = await _mk_session(db, "stu-1")
    current = await _mk_attempt(db, session_id=sess.id, problem_id="p1", result=None)
    await db.commit()

    assert await has_prior_graded_attempt(
        db=db,
        student_id="stu-1",
        problem_id="p1",
        exclude_attempt_id=current.id,
    ) is False


@pytest.mark.asyncio
async def test_returns_true_when_prior_session_has_graded_attempt(db):
    # First session/attempt: graded (solved).
    sess_a = await _mk_session(db, "stu-2")
    await _mk_attempt(db, session_id=sess_a.id, problem_id="p1", result="solved")

    # Second session/attempt: pending grade.
    sess_b = await _mk_session(db, "stu-2")
    current = await _mk_attempt(db, session_id=sess_b.id, problem_id="p1", result=None)
    await db.commit()

    assert await has_prior_graded_attempt(
        db=db,
        student_id="stu-2",
        problem_id="p1",
        exclude_attempt_id=current.id,
    ) is True


@pytest.mark.asyncio
async def test_ignores_other_students(db):
    sess_other = await _mk_session(db, "stu-other")
    await _mk_attempt(db, session_id=sess_other.id, problem_id="p1", result="solved")

    sess_mine = await _mk_session(db, "stu-me")
    current = await _mk_attempt(db, session_id=sess_mine.id, problem_id="p1", result=None)
    await db.commit()

    assert await has_prior_graded_attempt(
        db=db,
        student_id="stu-me",
        problem_id="p1",
        exclude_attempt_id=current.id,
    ) is False


@pytest.mark.asyncio
async def test_ignores_other_problems(db):
    sess = await _mk_session(db, "stu-3")
    await _mk_attempt(db, session_id=sess.id, problem_id="p-other", result="solved")

    sess2 = await _mk_session(db, "stu-3")
    current = await _mk_attempt(db, session_id=sess2.id, problem_id="p1", result=None)
    await db.commit()

    assert await has_prior_graded_attempt(
        db=db,
        student_id="stu-3",
        problem_id="p1",
        exclude_attempt_id=current.id,
    ) is False


@pytest.mark.asyncio
async def test_excludes_current_attempt_id(db):
    # Only one row exists and it has a result. We must not count ourselves
    # as a prior attempt.
    sess = await _mk_session(db, "stu-4")
    current = await _mk_attempt(db, session_id=sess.id, problem_id="p1", result="solved")
    await db.commit()

    assert await has_prior_graded_attempt(
        db=db,
        student_id="stu-4",
        problem_id="p1",
        exclude_attempt_id=current.id,
    ) is False
