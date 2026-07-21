"""V3 contract tests for handle_retry — post-grade retry starts a FRESH attempt.

Fresh-slate semantics (2026-07-13 prod hotfix): when the current attempt for
the session's problem is already resolved (``result`` non-null, i.e. the
student clicked retry from the report), ``handle_retry`` creates a NEW
``ProblemAttempt`` row for the same problem so the transcript/KG start empty
and Done grades only the new attempt (also making a second grading-artifact
insert impossible — the (attempt_id, role) unique key gets a new attempt_id).
When the current attempt is still in flight (``result`` null), retry stays a
pure phase flip so it can never wipe in-progress teaching.
"""

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.conftest import TEST_SPACE_ID, TEST_USER_ID
from apollo.handlers.lifecycle import handle_retry
from apollo.persistence.models import (
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
    TutoringMessage,
    TutoringSession,
)
from database.models import Base


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        execution_options={"schema_translate_map": {"app": None, "internal": None}},
    )
    tables = [
        TutoringSession.__table__,
        ProblemAttempt.__table__,
        TutoringMessage.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda sc: Base.metadata.create_all(sc, tables=tables))
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture
async def graded_session(db: AsyncSession):
    """Session in REPORT phase whose current attempt was graded (post-Done)."""
    sess = TutoringSession(
        user_id=TEST_USER_ID,
        search_space_id=TEST_SPACE_ID,
        concept_id=1,
        status=SessionStatus.active.value,
        phase=SessionPhase.REPORT.value,
        current_problem_id=1,
    )
    db.add(sess)
    await db.flush()
    attempt = ProblemAttempt(
        session_id=sess.id,
        problem_id=1,
        difficulty="intro",
        user_id=sess.user_id,
        course_id=sess.course_id,
        result="graded",
        diagnostic_report={"narrative": "first grade"},
    )
    db.add(attempt)
    await db.flush()
    db.add(
        TutoringMessage(
            session_id=sess.id,
            course_id=sess.course_id,
            attempt_id=attempt.id,
            role="student",
            content="first-attempt teaching",
            turn_index=0,
        )
    )
    await db.commit()
    await db.refresh(sess)
    await db.refresh(attempt)
    return db, sess, attempt


@pytest.mark.asyncio
async def test_retry_after_grade_creates_fresh_attempt(graded_session):
    db, sess, old_attempt = graded_session

    result = await handle_retry(db=db, session_id=int(sess.id))

    assert result["ok"] is True

    attempts = (
        (
            await db.execute(
                select(ProblemAttempt)
                .where(ProblemAttempt.session_id == sess.id)
                .order_by(ProblemAttempt.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(attempts) == 2
    fresh = attempts[-1]
    assert fresh.id != old_attempt.id
    assert result["attempt_id"] == fresh.id
    assert fresh.problem_id == "p1"
    assert fresh.difficulty == "intro"
    assert fresh.result is None
    assert fresh.diagnostic_report is None

    sess_after = (
        await db.execute(select(TutoringSession).where(TutoringSession.id == sess.id))
    ).scalar_one()
    assert sess_after.phase == SessionPhase.TEACHING.value


@pytest.mark.asyncio
async def test_retry_preserves_original_attempt_and_transcript(graded_session):
    db, sess, old_attempt = graded_session

    await handle_retry(db=db, session_id=int(sess.id))

    old_after = (
        await db.execute(select(ProblemAttempt).where(ProblemAttempt.id == old_attempt.id))
    ).scalar_one()
    assert old_after.result == "graded"
    assert old_after.diagnostic_report == {"narrative": "first grade"}

    old_msgs = (
        (await db.execute(select(TutoringMessage).where(TutoringMessage.attempt_id == old_attempt.id)))
        .scalars()
        .all()
    )
    assert len(old_msgs) == 1

    fresh_id = (
        (
            await db.execute(
                select(ProblemAttempt.id)
                .where(ProblemAttempt.session_id == sess.id)
                .order_by(ProblemAttempt.id.desc())
            )
        )
        .scalars()
        .first()
    )
    fresh_msgs = (
        (await db.execute(select(TutoringMessage).where(TutoringMessage.attempt_id == fresh_id))).scalars().all()
    )
    assert fresh_msgs == []


@pytest.mark.asyncio
async def test_retry_mid_teaching_keeps_current_attempt(graded_session):
    """In-flight attempt (result null) — retry must NOT spawn a new attempt."""
    db, sess, attempt = graded_session
    attempt.result = None
    sess.phase = SessionPhase.TEACHING.value
    await db.commit()

    result = await handle_retry(db=db, session_id=int(sess.id))

    assert result["ok"] is True
    assert result["attempt_id"] is None
    count = len(
        (await db.execute(select(ProblemAttempt).where(ProblemAttempt.session_id == sess.id)))
        .scalars()
        .all()
    )
    assert count == 1
    sess_after = (
        await db.execute(select(TutoringSession).where(TutoringSession.id == sess.id))
    ).scalar_one()
    assert sess_after.phase == SessionPhase.TEACHING.value


@pytest.mark.asyncio
async def test_retry_without_current_problem_phase_flips_only(db: AsyncSession):
    sess = TutoringSession(
        user_id=TEST_USER_ID,
        search_space_id=TEST_SPACE_ID,
        concept_id=1,
        status=SessionStatus.active.value,
        phase=SessionPhase.REPORT.value,
        current_problem_id=None,
    )
    db.add(sess)
    await db.commit()
    await db.refresh(sess)

    result = await handle_retry(db=db, session_id=int(sess.id))

    assert result == {"ok": True, "attempt_id": None}
    count = len(
        (await db.execute(select(ProblemAttempt).where(ProblemAttempt.session_id == sess.id)))
        .scalars()
        .all()
    )
    assert count == 0
    sess_after = (
        await db.execute(select(TutoringSession).where(TutoringSession.id == sess.id))
    ).scalar_one()
    assert sess_after.phase == SessionPhase.TEACHING.value
