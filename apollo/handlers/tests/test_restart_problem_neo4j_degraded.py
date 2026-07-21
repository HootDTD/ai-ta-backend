"""Apollo Neo4j degraded mode — `handle_restart_problem` (apollo/handlers/restart_problem.py).

NO silent skip on degradation: the wipe targets the SAME `attempt_id` (no
new `ProblemAttempt` row is created), so a skipped `delete_subgraph` would
resurface stale KG nodes once Neo4j returns. `delete_subgraph` failing
raises `KGUnavailableError` (-> structured 503 at the route layer) and the
Postgres `TutoringMessage` delete never runs — the ordering guarantee (wipe BEFORE
message delete) means nothing is half-wiped.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from neo4j.exceptions import ServiceUnavailable
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.conftest import TEST_SPACE_ID, TEST_USER_ID
from apollo.errors import KGUnavailableError
from apollo.handlers.restart_problem import handle_restart_problem
from apollo.persistence.models import (
    KGNegotiation,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
    TutoringMessage,
    TutoringSession,
)
from database.models import Base

pytestmark = pytest.mark.unit


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
        KGNegotiation.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda sc: Base.metadata.create_all(sc, tables=tables))
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture
async def session_teaching_with_messages(db: AsyncSession):
    sess = TutoringSession(
        user_id=TEST_USER_ID,
        search_space_id=TEST_SPACE_ID,
        concept_id=1,
        status=SessionStatus.active.value,
        phase=SessionPhase.TEACHING.value,
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
    )
    db.add(attempt)
    await db.flush()
    db.add(
        TutoringMessage(
            session_id=sess.id,
            course_id=sess.course_id,
            attempt_id=attempt.id,
            role="student",
            content="hi",
            turn_index=0,
        )
    )
    await db.commit()
    await db.refresh(sess)
    await db.refresh(attempt)
    return sess, attempt


@pytest.mark.asyncio
async def test_restart_raises_kg_unavailable_on_neo4j_down(db, session_teaching_with_messages):
    sess, attempt = session_teaching_with_messages

    with patch(
        "apollo.handlers.restart_problem.KGStore.delete_subgraph",
        new=AsyncMock(side_effect=ServiceUnavailable("aura down")),
    ):
        with pytest.raises(KGUnavailableError) as exc_info:
            await handle_restart_problem(db=db, neo=None, session_id=sess.id)

    assert exc_info.value.stage == "restart_problem"


@pytest.mark.asyncio
async def test_restart_degraded_leaves_messages_undeleted(db, session_teaching_with_messages):
    """Ordering guarantee: the Postgres TutoringMessage delete runs AFTER
    delete_subgraph, so a degraded wipe leaves messages intact — nothing is
    half-wiped."""
    sess, attempt = session_teaching_with_messages

    with patch(
        "apollo.handlers.restart_problem.KGStore.delete_subgraph",
        new=AsyncMock(side_effect=ServiceUnavailable("aura down")),
    ):
        with pytest.raises(KGUnavailableError):
            await handle_restart_problem(db=db, neo=None, session_id=sess.id)

    msgs = (
        (await db.execute(select(TutoringMessage).where(TutoringMessage.attempt_id == attempt.id))).scalars().all()
    )
    assert len(msgs) == 1


@pytest.mark.asyncio
async def test_restart_with_neo_none_raises_before_any_deletion():
    """With neo=None (no driver at all), the KGStore guard raises before
    the FastAPI route even reaches KG_DEGRADED_ERRORS — handle_restart_problem
    still surfaces KGUnavailableError via the same wrap."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        execution_options={"schema_translate_map": {"app": None, "internal": None}},
    )
    tables = [
        TutoringSession.__table__,
        ProblemAttempt.__table__,
        TutoringMessage.__table__,
        KGNegotiation.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda sc: Base.metadata.create_all(sc, tables=tables))
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as db:
        sess = TutoringSession(
            user_id=TEST_USER_ID,
            search_space_id=TEST_SPACE_ID,
            concept_id=1,
            status=SessionStatus.active.value,
            phase=SessionPhase.TEACHING.value,
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
        )
        db.add(attempt)
        await db.commit()
        await db.refresh(sess)

        with pytest.raises(KGUnavailableError) as exc_info:
            await handle_restart_problem(db=db, neo=None, session_id=sess.id)
        assert exc_info.value.stage == "restart_problem"

        msgs = (
            (await db.execute(select(TutoringMessage).where(TutoringMessage.attempt_id == attempt.id)))
            .scalars()
            .all()
        )
        assert msgs == []  # none existed; degraded path never got to delete anything
    await engine.dispose()
