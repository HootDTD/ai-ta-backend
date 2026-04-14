import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.handlers.lifecycle import handle_end, handle_retry
from apollo.persistence.models import ApolloSession, KGEntry, Message, ProblemAttempt, SessionPhase, SessionStatus
from database.models import Base


@pytest_asyncio.fixture
async def session_in_phase():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    apollo_tables = [
        ApolloSession.__table__,
        KGEntry.__table__,
        Message.__table__,
        ProblemAttempt.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=apollo_tables))
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async def _make(phase: SessionPhase):
        s = Session()
        sess = ApolloSession(
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

    sess = (await db.execute(select(ApolloSession).where(ApolloSession.id == session_id))).scalar_one()
    assert sess.phase == SessionPhase.TEACHING.value


@pytest.mark.asyncio
async def test_end_sets_status_ended(session_in_phase):
    db, session_id = await session_in_phase(SessionPhase.REPORT)

    result = await handle_end(db=db, session_id=session_id)
    assert result == {"ok": True}

    sess = (await db.execute(select(ApolloSession).where(ApolloSession.id == session_id))).scalar_one()
    assert sess.status == SessionStatus.ended.value


@pytest.mark.asyncio
async def test_get_session_returns_phase_kg_messages_and_current_problem(session_in_phase):
    from apollo.handlers.lifecycle import handle_get_session
    from apollo.persistence.models import KGEntry, Message

    db, session_id = await session_in_phase(SessionPhase.TEACHING)
    db.add(KGEntry(session_id=session_id, type="equation",
                   content={"symbolic": "A1*v1 - A2*v2", "label": "Continuity"},
                   source="parser"))
    db.add(Message(session_id=session_id, role="student", content="hi", turn_index=0))
    await db.commit()

    state = await handle_get_session(db=db, session_id=session_id)
    assert state["session_id"] == session_id
    assert state["phase"] == "TEACHING"
    assert state["concept_cluster_id"] == "fluid_mechanics"
    assert len(state["kg"]["equation"]) == 1
    assert len(state["messages"]) == 1
    assert state["problem"]["id"] == "bernoulli_horizontal_pipe_find_p2"
