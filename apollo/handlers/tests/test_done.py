from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.handlers.done import handle_done
from apollo.persistence.models import (
    ApolloSession,
    KGEntry,
    Message,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
)
from database.models import Base


@pytest_asyncio.fixture
async def db_with_session_and_kg():
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
    async with Session() as s:
        sess = ApolloSession(
            student_id="stu-1",
            concept_cluster_id="fluid_mechanics",
            status=SessionStatus.active.value,
            phase=SessionPhase.TEACHING.value,
            current_problem_id="bernoulli_horizontal_pipe_find_p2",
        )
        s.add(sess)
        await s.flush()
        s.add(ProblemAttempt(
            session_id=sess.id,
            problem_id="bernoulli_horizontal_pipe_find_p2",
            difficulty="intro",
        ))
        for entry in [
            {"type": "equation", "content": {"symbolic": "rho*A1*v1 - rho*A2*v2", "label": "Continuity"}},
            {"type": "equation", "content": {
                "symbolic": "P1 + Rational(1,2)*rho*v1**2 + rho*g*h1 - (P2 + Rational(1,2)*rho*v2**2 + rho*g*h2)",
                "label": "Bernoulli",
            }},
        ]:
            s.add(KGEntry(session_id=sess.id, type=entry["type"], content=entry["content"], source="parser"))
        await s.commit()
        await s.refresh(sess)
        yield s, sess.id
    await engine.dispose()


@pytest.mark.asyncio
@patch("apollo.handlers.done.generate_diagnostic")
async def test_done_solved_returns_value_194000(mock_diag, db_with_session_and_kg):
    mock_diag.return_value = "You taught it well — Apollo solved the problem."
    db, session_id = db_with_session_and_kg

    result = await handle_done(db=db, session_id=session_id)

    assert result["result"] == "solved"
    assert abs(float(result["value"]) - 194000.0) < 1e-3
    assert "narrated_trace" in result
    assert "diagnostic_report" in result


@pytest.mark.asyncio
@patch("apollo.handlers.done.generate_diagnostic")
async def test_done_freezes_session_and_persists_attempt(mock_diag, db_with_session_and_kg):
    mock_diag.return_value = "report"
    db, session_id = db_with_session_and_kg

    await handle_done(db=db, session_id=session_id)

    from sqlalchemy import select
    sess = (await db.execute(select(ApolloSession).where(ApolloSession.id == session_id))).scalar_one()
    assert sess.phase == SessionPhase.REPORT.value

    pa = (await db.execute(select(ProblemAttempt).where(ProblemAttempt.session_id == session_id))).scalar_one()
    assert pa.result == "solved"
    assert pa.solver_trace is not None
    assert pa.diagnostic_report is not None
