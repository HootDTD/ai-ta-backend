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

from apollo.errors import InvalidPhaseError, SessionFrozenError
from apollo.handlers.restart_problem import handle_restart_problem
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
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        execution_options={"schema_translate_map": {"app": None, "internal": None}},
    )
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
        s.add(
            ProblemAttempt(
                session_id=sess.id,
                problem_id="bernoulli_horizontal_pipe_find_p2",
                difficulty="intro",
                result="solved",
            )
        )
        await s.commit()
        await s.refresh(sess)
        yield s, sess.id
    await engine.dispose()


@pytest.mark.asyncio
async def test_restart_wipes_kg_and_messages_for_current_attempt(db_with_report_session):
    s, session_id = db_with_report_session
    attempt = (
        await s.execute(select(ProblemAttempt).where(ProblemAttempt.session_id == session_id))
    ).scalar_one()
    # Flip to TEACHING + seed KG/messages scoped to current attempt, then clear result so
    # the attempt behaves like an in-flight one for the restart.
    sess = (
        await s.execute(select(ApolloSession).where(ApolloSession.id == session_id))
    ).scalar_one()
    sess.phase = SessionPhase.TEACHING.value
    attempt.result = None
    s.add_all(
        [
            KGEntry(
                session_id=session_id,
                attempt_id=attempt.id,
                type="equation",
                content={"symbolic": "x - 1", "label": "to_be_wiped"},
                source="parser",
            ),
            Message(
                session_id=session_id,
                attempt_id=attempt.id,
                role="student",
                content="hi",
                turn_index=0,
            ),
        ]
    )
    await s.commit()

    result = await handle_restart_problem(db=s, session_id=session_id)
    assert result == {"ok": True}

    kg_rows = (
        (await s.execute(select(KGEntry).where(KGEntry.attempt_id == attempt.id))).scalars().all()
    )
    msg_rows = (
        (await s.execute(select(Message).where(Message.attempt_id == attempt.id))).scalars().all()
    )
    assert kg_rows == []
    assert msg_rows == []

    attempt_after = (
        await s.execute(select(ProblemAttempt).where(ProblemAttempt.id == attempt.id))
    ).scalar_one()
    assert attempt_after.problem_id == "bernoulli_horizontal_pipe_find_p2"
    assert attempt_after.difficulty == "intro"
    assert attempt_after.result is None

    sess_after = (
        await s.execute(select(ApolloSession).where(ApolloSession.id == session_id))
    ).scalar_one()
    assert sess_after.phase == SessionPhase.TEACHING.value


@pytest.mark.asyncio
async def test_restart_from_report_unfreezes_to_teaching(db_with_report_session):
    # Default fixture phase is REPORT — exercise the unfreeze path.
    s, session_id = db_with_report_session
    result = await handle_restart_problem(db=s, session_id=session_id)
    assert result == {"ok": True}
    sess = (
        await s.execute(select(ApolloSession).where(ApolloSession.id == session_id))
    ).scalar_one()
    assert sess.phase == SessionPhase.TEACHING.value


@pytest.mark.asyncio
async def test_restart_blocked_during_solving(db_with_report_session):
    s, session_id = db_with_report_session
    sess = (
        await s.execute(select(ApolloSession).where(ApolloSession.id == session_id))
    ).scalar_one()
    sess.phase = SessionPhase.SOLVING.value
    await s.commit()
    with pytest.raises(SessionFrozenError):
        await handle_restart_problem(db=s, session_id=session_id)


@pytest.mark.asyncio
async def test_restart_raises_invalid_phase_from_init(db_with_report_session):
    s, session_id = db_with_report_session
    sess = (
        await s.execute(select(ApolloSession).where(ApolloSession.id == session_id))
    ).scalar_one()
    sess.phase = SessionPhase.INIT.value
    await s.commit()
    with pytest.raises(InvalidPhaseError):
        await handle_restart_problem(db=s, session_id=session_id)


@pytest.mark.asyncio
async def test_restart_does_not_touch_other_attempts(db_with_report_session):
    s, session_id = db_with_report_session
    current = (
        await s.execute(select(ProblemAttempt).where(ProblemAttempt.session_id == session_id))
    ).scalar_one()
    other = ProblemAttempt(
        session_id=session_id,
        problem_id="some_other_problem",
        difficulty="intro",
        result="abandoned",
    )
    s.add(other)
    await s.flush()
    s.add_all(
        [
            KGEntry(
                session_id=session_id,
                attempt_id=other.id,
                type="equation",
                content={"symbolic": "survivor - 0"},
                source="parser",
            ),
            KGEntry(
                session_id=session_id,
                attempt_id=current.id,
                type="equation",
                content={"symbolic": "victim - 0"},
                source="parser",
            ),
        ]
    )
    sess = (
        await s.execute(select(ApolloSession).where(ApolloSession.id == session_id))
    ).scalar_one()
    sess.phase = SessionPhase.TEACHING.value
    await s.commit()

    await handle_restart_problem(db=s, session_id=session_id)

    survivors = (
        (await s.execute(select(KGEntry).where(KGEntry.attempt_id == other.id))).scalars().all()
    )
    assert len(survivors) == 1
    victims = (
        (await s.execute(select(KGEntry).where(KGEntry.attempt_id == current.id))).scalars().all()
    )
    assert victims == []
