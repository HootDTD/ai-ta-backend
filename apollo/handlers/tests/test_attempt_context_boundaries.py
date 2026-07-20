from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.conftest import TEST_SPACE_ID, TEST_USER_ID
from apollo.handlers.next import handle_next
from apollo.handlers.restart_problem import handle_restart_problem
from apollo.persistence.models import (
    TutoringSession,
    TutoringMessage,
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
            lambda sync_conn: Base.metadata.create_all(
                sync_conn,
                tables=[
                    TutoringSession.__table__,
                    ProblemAttempt.__table__,
                    TutoringMessage.__table__,
                ],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _seed(db: AsyncSession, *, phase: str):
    session = TutoringSession(
        user_id=TEST_USER_ID,
        search_space_id=TEST_SPACE_ID,
        concept_id=1,
        status=SessionStatus.active.value,
        phase=phase,
        current_problem_id="p1",
        pending_intent="done",
        history_summary="old summary",
        history_summary_up_to_turn=7,
    )
    db.add(session)
    await db.flush()
    attempt = ProblemAttempt(
        session_id=session.id,
        problem_id="p1",
        difficulty="intro",
    )
    db.add(attempt)
    await db.commit()
    return session, attempt


def _assert_context_cleared(session: TutoringSession) -> None:
    assert session.pending_intent is None
    assert session.history_summary is None
    assert session.history_summary_up_to_turn is None


@pytest.mark.asyncio
async def test_next_problem_clears_session_scoped_attempt_context(db):
    session, _ = await _seed(db, phase=SessionPhase.TEACHING.value)
    problem = SimpleNamespace(
        id="p2",
        concept_id=1,
        difficulty="intro",
        problem_text="new",
        given_values={},
        target_unknown="x",
    )

    with patch(
        "apollo.handlers.next.select_problem_personalized",
        new=AsyncMock(return_value=problem),
    ):
        response = await handle_next(
            db=db,
            session_id=session.id,
            difficulty="intro",
        )

    assert response["problem"]["id"] == "p2"
    _assert_context_cleared(session)


@pytest.mark.asyncio
async def test_restart_problem_clears_session_scoped_attempt_context(db):
    session, _ = await _seed(db, phase=SessionPhase.TEACHING.value)

    with patch(
        "apollo.handlers.restart_problem.KGStore.delete_subgraph",
        new=AsyncMock(return_value=None),
    ):
        response = await handle_restart_problem(
            db=db,
            neo=object(),
            session_id=session.id,
        )

    assert response == {"ok": True}
    _assert_context_cleared(session)
