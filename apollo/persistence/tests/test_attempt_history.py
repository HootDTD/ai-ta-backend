from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.conftest import TEST_SPACE_ID
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
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        execution_options={"schema_translate_map": {"app": None, "internal": None}},
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sc: Base.metadata.create_all(
                sc, tables=[ApolloSession.__table__, ProblemAttempt.__table__]
            )
        )
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        yield s
    await engine.dispose()


async def _mk_session(
    db: AsyncSession,
    user_id: str,
    *,
    status: str = SessionStatus.active.value,
) -> ApolloSession:
    s = ApolloSession(
        user_id=user_id,
        search_space_id=TEST_SPACE_ID,
        concept_id=1,
        status=status,
        phase=SessionPhase.TEACHING.value,
        current_problem_id="p1",
    )
    db.add(s)
    await db.flush()
    return s


async def _mk_attempt(
    db: AsyncSession, *, session_id: int, problem_id: str, result: str | None
) -> ProblemAttempt:
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

    assert (
        await has_prior_graded_attempt(
            db=db,
            user_id="stu-1",
            problem_id="p1",
            exclude_attempt_id=current.id,
        )
        is False
    )


@pytest.mark.asyncio
async def test_returns_true_when_prior_session_has_graded_attempt(db):
    # First session/attempt: graded (solved). Prior session is ended.
    sess_a = await _mk_session(db, "stu-2", status=SessionStatus.ended.value)
    await _mk_attempt(db, session_id=sess_a.id, problem_id="p1", result="solved")

    # Second session/attempt: pending grade.
    sess_b = await _mk_session(db, "stu-2")
    current = await _mk_attempt(db, session_id=sess_b.id, problem_id="p1", result=None)
    await db.commit()

    assert (
        await has_prior_graded_attempt(
            db=db,
            user_id="stu-2",
            problem_id="p1",
            exclude_attempt_id=current.id,
        )
        is True
    )


@pytest.mark.asyncio
async def test_ignores_other_users(db):
    sess_other = await _mk_session(db, "stu-other")
    await _mk_attempt(db, session_id=sess_other.id, problem_id="p1", result="solved")

    sess_mine = await _mk_session(db, "stu-me")
    current = await _mk_attempt(db, session_id=sess_mine.id, problem_id="p1", result=None)
    await db.commit()

    assert (
        await has_prior_graded_attempt(
            db=db,
            user_id="stu-me",
            problem_id="p1",
            exclude_attempt_id=current.id,
        )
        is False
    )


@pytest.mark.asyncio
async def test_ignores_other_problems(db):
    sess = await _mk_session(db, "stu-3", status=SessionStatus.ended.value)
    await _mk_attempt(db, session_id=sess.id, problem_id="p-other", result="solved")

    sess2 = await _mk_session(db, "stu-3")
    current = await _mk_attempt(db, session_id=sess2.id, problem_id="p1", result=None)
    await db.commit()

    assert (
        await has_prior_graded_attempt(
            db=db,
            user_id="stu-3",
            problem_id="p1",
            exclude_attempt_id=current.id,
        )
        is False
    )


@pytest.mark.asyncio
async def test_excludes_current_attempt_id(db):
    # Only one row exists and it has a result. We must not count ourselves
    # as a prior attempt.
    sess = await _mk_session(db, "stu-4")
    current = await _mk_attempt(db, session_id=sess.id, problem_id="p1", result="solved")
    await db.commit()

    assert (
        await has_prior_graded_attempt(
            db=db,
            user_id="stu-4",
            problem_id="p1",
            exclude_attempt_id=current.id,
        )
        is False
    )


@pytest.mark.asyncio
async def test_has_prior_graded_attempt_excludes_abandoned(db):
    # Previous attempt was abandoned (user switched problems mid-teach).
    sess_a = await _mk_session(db, "stu-1", status=SessionStatus.ended.value)
    await _mk_attempt(db, session_id=sess_a.id, problem_id="p1", result="abandoned")
    # Current attempt on same problem, not yet graded.
    sess_b = await _mk_session(db, "stu-1")
    current = await _mk_attempt(db, session_id=sess_b.id, problem_id="p1", result=None)
    await db.commit()

    result = await has_prior_graded_attempt(
        db=db,
        user_id="stu-1",
        problem_id="p1",
        exclude_attempt_id=current.id,
    )
    assert result is False, "abandoned attempts must not count as prior grades"


@pytest.mark.asyncio
async def test_counts_graded_result(db):
    # Since the solver was dropped (commit 21b42e1), a completed grade is stored
    # as result='graded'. A prior 'graded' attempt MUST count as a prior grade,
    # otherwise the re-attempt XP multiplier silently never fires.
    sess_a = await _mk_session(db, "stu-5", status=SessionStatus.ended.value)
    await _mk_attempt(db, session_id=sess_a.id, problem_id="p1", result="graded")
    sess_b = await _mk_session(db, "stu-5")
    current = await _mk_attempt(db, session_id=sess_b.id, problem_id="p1", result=None)
    await db.commit()

    result = await has_prior_graded_attempt(
        db=db,
        user_id="stu-5",
        problem_id="p1",
        exclude_attempt_id=current.id,
    )
    assert result is True, "a prior graded attempt must count as a prior grade"
