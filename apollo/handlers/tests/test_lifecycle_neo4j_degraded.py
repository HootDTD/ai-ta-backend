"""Apollo Neo4j degraded mode — `handle_get_session` (apollo/handlers/lifecycle.py).

`handle_get_session`'s KG read degrades to `{"nodes": [], "edges": []}` on a
`KG_DEGRADED_ERRORS` failure — the same literal the existing "no current
attempt" else-branch already returns — while the rest of the payload
(session/problem/messages) stays intact. `handle_end` has NO Neo4j
involvement (its `neo` param is unused) — no test needed per the plan.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from neo4j.exceptions import ServiceUnavailable
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.conftest import TEST_SPACE_ID, TEST_USER_ID
from apollo.handlers.lifecycle import handle_get_session
from apollo.persistence.models import (
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
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda sc: Base.metadata.create_all(sc, tables=tables))
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture
async def session_with_attempt(db: AsyncSession):
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
async def test_get_session_degrades_kg_read_on_neo4j_down(db, session_with_attempt):
    sess, attempt = session_with_attempt

    with (
        patch(
            "apollo.handlers.lifecycle.KGStore.read_graph",
            new=AsyncMock(side_effect=ServiceUnavailable("aura down")),
        ),
        patch(
            "apollo.handlers.lifecycle.list_problems_for_concept",
            new=AsyncMock(return_value=[]),
        ),
    ):
        out = await handle_get_session(db=db, neo=None, session_id=sess.id)

    assert out["kg"] == {"nodes": [], "edges": []}
    # Rest of the payload is intact — same shape as a healthy call.
    assert out["session_id"] == sess.id
    assert out["status"] == SessionStatus.active.value
    assert out["phase"] == SessionPhase.TEACHING.value
    assert len(out["messages"]) == 1
    assert out["messages"][0]["content"] == "hi"


@pytest.mark.asyncio
async def test_get_session_healthy_read_unaffected(db, session_with_attempt):
    """Regression: a healthy read_graph is unaffected by the try/except
    added around it — byte-identical to before."""
    from apollo.ontology import KGGraph

    sess, attempt = session_with_attempt

    with (
        patch(
            "apollo.handlers.lifecycle.KGStore.read_graph",
            new=AsyncMock(return_value=KGGraph()),
        ) as mock_read,
        patch(
            "apollo.handlers.lifecycle.list_problems_for_concept",
            new=AsyncMock(return_value=[]),
        ),
    ):
        out = await handle_get_session(db=db, neo=None, session_id=sess.id)

    mock_read.assert_awaited_once()
    assert out["kg"] == {"nodes": [], "edges": []}
