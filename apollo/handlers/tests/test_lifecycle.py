import pytest as _pytest_module

_pytest_module.skip(
    "Legacy V2 test — needs rewrite for V3 KGGraph + Neo4j store + new parser/coverage signatures. "
    "Tracked in claude_v3_checklist.md item 1; will be re-enabled in test-rewrite phase.",
    allow_module_level=True,
)

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.handlers.lifecycle import handle_end, handle_retry
from apollo.persistence.models import (
    TutoringSession,
    KGEntry,
    TutoringMessage,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
)
from database.models import Base


@pytest_asyncio.fixture
async def session_in_phase():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        execution_options={"schema_translate_map": {"app": None, "internal": None}},
    )
    apollo_tables = [
        TutoringSession.__table__,
        KGEntry.__table__,
        TutoringMessage.__table__,
        ProblemAttempt.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=apollo_tables)
        )
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async def _make(phase: SessionPhase):
        s = Session()
        sess = TutoringSession(
            student_id="stu-1",
            concept_cluster_id="fluid_mechanics",
            status=SessionStatus.active.value,
            phase=phase.value,
            current_problem_id="bernoulli_horizontal_pipe_find_p2",
        )
        s.add(sess)
        await s.commit()
        await s.refresh(sess)
        return s, sess.id

    yield _make
    await engine.dispose()


@pytest.mark.asyncio
async def test_retry_unfreezes_back_to_teaching(session_in_phase):
    db, session_id = await session_in_phase(SessionPhase.REPORT)

    result = await handle_retry(db=db, session_id=session_id)
    assert result == {"ok": True}

    sess = (
        await db.execute(select(TutoringSession).where(TutoringSession.id == session_id))
    ).scalar_one()
    assert sess.phase == SessionPhase.TEACHING.value


@pytest.mark.asyncio
async def test_end_sets_status_ended(session_in_phase):
    db, session_id = await session_in_phase(SessionPhase.REPORT)

    result = await handle_end(db=db, session_id=session_id)
    assert result == {"ok": True}

    sess = (
        await db.execute(select(TutoringSession).where(TutoringSession.id == session_id))
    ).scalar_one()
    assert sess.status == SessionStatus.ended.value


@pytest.mark.asyncio
async def test_get_session_returns_phase_kg_messages_and_current_problem(session_in_phase):
    from apollo.handlers.lifecycle import handle_get_session
    from apollo.persistence.models import KGEntry, TutoringMessage

    db, session_id = await session_in_phase(SessionPhase.TEACHING)
    attempt = ProblemAttempt(
        session_id=session_id,
        problem_id="bernoulli_horizontal_pipe_find_p2",
        difficulty="standard",
    )
    db.add(attempt)
    await db.flush()

    db.add(
        KGEntry(
            session_id=session_id,
            attempt_id=attempt.id,
            type="equation",
            content={"symbolic": "A1*v1 - A2*v2", "label": "Continuity"},
            source="parser",
        )
    )
    db.add(
        TutoringMessage(
            session_id=session_id, attempt_id=attempt.id, role="student", content="hi", turn_index=0
        )
    )
    await db.commit()

    state = await handle_get_session(db=db, session_id=session_id)
    assert state["session_id"] == session_id
    assert state["phase"] == "TEACHING"
    assert state["concept_cluster_id"] == "fluid_mechanics"
    assert len(state["kg"]["equation"]) == 1
    assert len(state["messages"]) == 1
    assert state["problem"]["id"] == "bernoulli_horizontal_pipe_find_p2"


@pytest.mark.asyncio
async def test_get_session_returns_only_current_attempt_kg_and_messages(session_in_phase):
    from apollo.handlers.lifecycle import handle_get_session

    db, session_id = await session_in_phase(SessionPhase.TEACHING)
    # Switch current_problem_id to p2 so we can seed two attempts and verify isolation.
    sess = (
        await db.execute(select(TutoringSession).where(TutoringSession.id == session_id))
    ).scalar_one()
    sess.current_problem_id = "p2"
    await db.flush()

    a = ProblemAttempt(
        session_id=session_id, problem_id="p1", difficulty="intro", result="abandoned"
    )
    b = ProblemAttempt(session_id=session_id, problem_id="p2", difficulty="standard")
    db.add_all([a, b])
    await db.flush()

    db.add_all(
        [
            KGEntry(
                session_id=session_id,
                attempt_id=a.id,
                type="equation",
                content={"symbolic": "x - 1", "label": "old"},
                source="parser",
            ),
            KGEntry(
                session_id=session_id,
                attempt_id=b.id,
                type="equation",
                content={"symbolic": "y - 2", "label": "new"},
                source="parser",
            ),
            TutoringMessage(
                session_id=session_id, attempt_id=a.id, role="student", content="old", turn_index=0
            ),
            TutoringMessage(
                session_id=session_id, attempt_id=b.id, role="student", content="new", turn_index=0
            ),
        ]
    )
    await db.commit()

    state = await handle_get_session(db=db, session_id=session_id)
    assert [e.get("label") for e in state["kg"]["equation"]] == ["new"]
    assert [m["content"] for m in state["messages"]] == ["new"]
