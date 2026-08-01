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
    Concept,
    Problem,
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
        Concept.__table__,
        Problem.__table__,
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
        concept_id=None,
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

    with patch(
        "apollo.handlers.lifecycle.KGStore.read_graph",
        new=AsyncMock(side_effect=ServiceUnavailable("aura down")),
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

    with patch(
        "apollo.handlers.lifecycle.KGStore.read_graph",
        new=AsyncMock(return_value=KGGraph()),
    ) as mock_read:
        out = await handle_get_session(db=db, neo=None, session_id=sess.id)

    mock_read.assert_awaited_once()
    assert out["kg"] == {"nodes": [], "edges": []}


@pytest.mark.asyncio
async def test_get_session_fetches_and_serializes_only_current_problem(db, monkeypatch):
    concept = Concept(
        course_id=TEST_SPACE_ID,
        subject_slug="physics",
        subject_display_name="Physics",
        slug="newton-2",
        display_name="Newton's Second Law",
    )
    db.add(concept)
    await db.flush()

    def _payload(code: str) -> dict:
        return {
            "id": code,
            "concept_id": "newton-2",
            "difficulty": "intro",
            "problem_text": f"Problem {code}",
            "given_values": {"m": 2.0, "a": 3.0},
            "target_unknown": "F",
            "reference_solution": [
                {
                    "step": 1,
                    "entry_type": "equation",
                    "id": f"{code}-step",
                    "content": {"symbolic": "F = m*a"},
                }
            ],
        }

    current = Problem.from_pydantic_payload(
        _payload("current"),
        course_id=TEST_SPACE_ID,
        concept_id=concept.id,
        tier=2,
    )
    sibling = Problem.from_pydantic_payload(
        _payload("sibling"),
        course_id=TEST_SPACE_ID,
        concept_id=concept.id,
        tier=2,
    )
    db.add_all([current, sibling])
    await db.flush()
    sess = TutoringSession(
        user_id=TEST_USER_ID,
        search_space_id=TEST_SPACE_ID,
        concept_id=concept.id,
        status=SessionStatus.active.value,
        phase=SessionPhase.TEACHING.value,
        current_problem_id=current.id,
    )
    db.add(sess)
    await db.commit()

    original_serializer = Problem.to_pydantic_payload

    def _only_current_is_serialized(row, *, concept_slug):
        assert row.id == current.id
        return original_serializer(row, concept_slug=concept_slug)

    monkeypatch.setattr(Problem, "to_pydantic_payload", _only_current_is_serialized)

    out = await handle_get_session(db=db, neo=None, session_id=sess.id)

    assert out["problem"] == {
        "id": "current",
        "concept_id": "newton-2",
        "difficulty": "intro",
        "problem_text": "Problem current",
        "given_values": {"m": 2.0, "a": 3.0},
        "target_unknown": "F",
    }
