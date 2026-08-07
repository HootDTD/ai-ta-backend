"""restart_problem wipes the QuestionOpportunity ledger with the transcript.

Defect I4 (2026-08-07 bimodal-fix spec): the wipe deleted the Neo4j subgraph +
TutoringMessage rows but left QuestionOpportunity rows behind. The questioning
controller reloads the ledger by attempt_id, so stale `times_asked` survived a
restart — if pre-restart asks already hit the per-node cap, the first
post-restart message exhausted the budget and auto-graded a one-message
transcript. The ledger delete must be scoped to the CURRENT attempt and must
NOT run when the KG wipe fails (nothing half-wiped).
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
    ProblemAttempt,
    QuestionOpportunity,
    SessionPhase,
    SessionStatus,
    TutoringMessage,
    TutoringSession,
)
from database.models import Base

pytestmark = pytest.mark.unit


def _opportunity(sess: TutoringSession, attempt_id: int, node_id: str) -> QuestionOpportunity:
    return QuestionOpportunity(
        course_id=sess.course_id,
        session_id=sess.id,
        attempt_id=attempt_id,
        reference_node_id=node_id,
        state="asked_waiting",
        question="What does this step mean?",
        times_asked=2,
    )


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
        QuestionOpportunity.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda sc: Base.metadata.create_all(sc, tables=tables))
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture
async def teaching_session_with_ledger(db: AsyncSession):
    """A TEACHING session whose current attempt has messages + ledger rows,
    plus a PRIOR attempt whose ledger rows must survive the restart."""
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
    prior = ProblemAttempt(
        session_id=sess.id,
        problem_id=1,
        difficulty="intro",
        user_id=sess.user_id,
        course_id=sess.course_id,
        result="graded",
    )
    db.add(prior)
    await db.flush()
    current = ProblemAttempt(
        session_id=sess.id,
        problem_id=1,
        difficulty="intro",
        user_id=sess.user_id,
        course_id=sess.course_id,
    )
    db.add(current)
    await db.flush()
    db.add(
        TutoringMessage(
            session_id=sess.id,
            course_id=sess.course_id,
            attempt_id=current.id,
            role="student",
            content="hi",
            turn_index=0,
        )
    )
    db.add(_opportunity(sess, int(prior.id), "q1_prior_node"))
    db.add(_opportunity(sess, int(current.id), "q1_current_node"))
    db.add(_opportunity(sess, int(current.id), "q2_current_node"))
    await db.commit()
    await db.refresh(sess)
    await db.refresh(prior)
    await db.refresh(current)
    return sess, prior, current


@pytest.mark.asyncio
async def test_restart_deletes_current_attempt_ledger(db, teaching_session_with_ledger):
    sess, prior, current = teaching_session_with_ledger

    with patch(
        "apollo.handlers.restart_problem.KGStore.delete_subgraph",
        new=AsyncMock(return_value=None),
    ):
        result = await handle_restart_problem(db=db, neo=None, session_id=sess.id)

    assert result == {"ok": True}
    current_rows = (
        (
            await db.execute(
                select(QuestionOpportunity).where(QuestionOpportunity.attempt_id == current.id)
            )
        )
        .scalars()
        .all()
    )
    assert current_rows == []
    msgs = (
        (await db.execute(select(TutoringMessage).where(TutoringMessage.attempt_id == current.id)))
        .scalars()
        .all()
    )
    assert msgs == []


@pytest.mark.asyncio
async def test_restart_keeps_other_attempt_ledger(db, teaching_session_with_ledger):
    sess, prior, current = teaching_session_with_ledger

    with patch(
        "apollo.handlers.restart_problem.KGStore.delete_subgraph",
        new=AsyncMock(return_value=None),
    ):
        await handle_restart_problem(db=db, neo=None, session_id=sess.id)

    prior_rows = (
        (
            await db.execute(
                select(QuestionOpportunity).where(QuestionOpportunity.attempt_id == prior.id)
            )
        )
        .scalars()
        .all()
    )
    assert [row.reference_node_id for row in prior_rows] == ["q1_prior_node"]


@pytest.mark.asyncio
async def test_degraded_restart_leaves_ledger_intact(db, teaching_session_with_ledger):
    """Ordering guarantee extends to the ledger: a failed KG wipe raises
    before ANY Postgres delete, so messages AND opportunities both survive."""
    sess, prior, current = teaching_session_with_ledger

    with patch(
        "apollo.handlers.restart_problem.KGStore.delete_subgraph",
        new=AsyncMock(side_effect=ServiceUnavailable("aura down")),
    ):
        with pytest.raises(KGUnavailableError):
            await handle_restart_problem(db=db, neo=None, session_id=sess.id)

    current_rows = (
        (
            await db.execute(
                select(QuestionOpportunity).where(QuestionOpportunity.attempt_id == current.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(current_rows) == 2
