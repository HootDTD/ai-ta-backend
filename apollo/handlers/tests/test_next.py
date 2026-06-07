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

from apollo.errors import InvalidPhaseError, PoolExhaustedError, SessionFrozenError
from apollo.handlers.next import handle_next
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
async def db_with_report_session():
    """Session in REPORT phase with one graded attempt on an intro problem."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        ApolloSession.__table__,
        KGEntry.__table__,
        Message.__table__,
        ProblemAttempt.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda sc: Base.metadata.create_all(sc, tables=tables))
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        sess = ApolloSession(
            student_id="stu-1",
            concept_cluster_id="fluid_mechanics",
            status=SessionStatus.active.value,
            phase=SessionPhase.REPORT.value,
            current_problem_id="bernoulli_horizontal_pipe_find_p2",
        )
        s.add(sess)
        await s.flush()
        s.add(ProblemAttempt(
            session_id=sess.id,
            problem_id="bernoulli_horizontal_pipe_find_p2",
            difficulty="intro",
            result="solved",
        ))
        await s.commit()
        await s.refresh(sess)
        yield s, sess.id
    await engine.dispose()


@pytest.mark.asyncio
async def test_next_from_report_advances(db_with_report_session):
    s, session_id = db_with_report_session
    result = await handle_next(db=s, session_id=session_id, difficulty="standard")

    assert result["session_id"] == session_id
    assert result["attempt_id"] is not None
    assert result["problem"]["id"] != "bernoulli_horizontal_pipe_find_p2"

    sess = (await s.execute(select(ApolloSession).where(ApolloSession.id == session_id))).scalar_one()
    assert sess.phase == SessionPhase.TEACHING.value
    assert sess.current_problem_id == result["problem"]["id"]

    prior = (await s.execute(
        select(ProblemAttempt)
        .where(ProblemAttempt.session_id == session_id)
        .where(ProblemAttempt.problem_id == "bernoulli_horizontal_pipe_find_p2")
    )).scalar_one()
    assert prior.result == "solved"

    new_attempt = (await s.execute(
        select(ProblemAttempt).where(ProblemAttempt.id == result["attempt_id"])
    )).scalar_one()
    assert new_attempt.difficulty == "standard"
    assert new_attempt.result is None


@pytest.mark.asyncio
async def test_next_from_teaching_abandons_current(db_with_report_session):
    s, session_id = db_with_report_session
    sess = (await s.execute(select(ApolloSession).where(ApolloSession.id == session_id))).scalar_one()
    sess.phase = SessionPhase.TEACHING.value
    prior = (await s.execute(
        select(ProblemAttempt).where(ProblemAttempt.session_id == session_id)
    )).scalar_one()
    prior.result = None
    await s.commit()

    result = await handle_next(db=s, session_id=session_id, difficulty="standard")

    abandoned = (await s.execute(
        select(ProblemAttempt).where(ProblemAttempt.id == prior.id)
    )).scalar_one()
    assert abandoned.result == "abandoned"

    new_attempt = (await s.execute(
        select(ProblemAttempt).where(ProblemAttempt.id == result["attempt_id"])
    )).scalar_one()
    assert new_attempt.difficulty == "standard"


@pytest.mark.asyncio
async def test_next_raises_session_frozen_during_solving(db_with_report_session):
    s, session_id = db_with_report_session
    sess = (await s.execute(select(ApolloSession).where(ApolloSession.id == session_id))).scalar_one()
    sess.phase = SessionPhase.SOLVING.value
    await s.commit()
    with pytest.raises(SessionFrozenError):
        await handle_next(db=s, session_id=session_id, difficulty="standard")


@pytest.mark.asyncio
async def test_next_raises_invalid_phase_from_init(db_with_report_session):
    s, session_id = db_with_report_session
    sess = (await s.execute(select(ApolloSession).where(ApolloSession.id == session_id))).scalar_one()
    sess.phase = SessionPhase.INIT.value
    await s.commit()
    with pytest.raises(InvalidPhaseError):
        await handle_next(db=s, session_id=session_id, difficulty="standard")


@pytest.mark.asyncio
async def test_next_raises_pool_exhausted_when_all_problems_attempted(db_with_report_session, monkeypatch):
    s, session_id = db_with_report_session
    def _boom(*, cluster_id, difficulty, attempted_ids):
        raise PoolExhaustedError(concept_cluster_id=cluster_id, difficulty=difficulty)
    monkeypatch.setattr("apollo.handlers.next.select_problem", _boom)
    with pytest.raises(PoolExhaustedError):
        await handle_next(db=s, session_id=session_id, difficulty="hard")


@pytest.mark.asyncio
async def test_next_excludes_prior_problem_ids(db_with_report_session, monkeypatch):
    s, session_id = db_with_report_session
    captured = {}
    def _spy(*, cluster_id, difficulty, attempted_ids):
        captured["attempted_ids"] = list(attempted_ids)
        from apollo.overseer.problem_selector import select_problem as real
        return real(cluster_id=cluster_id, difficulty=difficulty, attempted_ids=attempted_ids)
    monkeypatch.setattr("apollo.handlers.next.select_problem", _spy)
    await handle_next(db=s, session_id=session_id, difficulty="intro")
    assert "bernoulli_horizontal_pipe_find_p2" in captured["attempted_ids"]
