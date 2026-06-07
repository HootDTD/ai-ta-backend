import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.errors import InvalidPhaseError, PoolExhaustedError, SessionFrozenError
from apollo.handlers.next import handle_next
from apollo.persistence.models import (
    ApolloSession,
    Message,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
)
from apollo.schemas.problem import ReferenceStep
from apollo.subjects import CanonicalSymbols, SolverHints
from apollo.textbook_ingest import writer
from apollo.textbook_ingest.types import ConceptRegistryEntry, ValidatedProblem
from database.models import Base


CURRENT_PROBLEM_ID = "seed_intro_p1"


async def _seed_problem(neo, *, pid, difficulty):
    await writer.write_problem(neo, ValidatedProblem(
        source_document_id="seed", source_chunk_id=pid, source_page=1,
        problem_text="t", given_values={"P1": 1.0}, target_unknown="P2",
        reference_solution=[ReferenceStep(step=1, entry_type="equation", id="e1",
            content={"symbolic": "P1 - P2", "label": "x", "variables": ["P1", "P2"]},
            depends_on=[])],
        concept_id="bernoulli_principle", subject_id="fluid_mechanics",
        difficulty=difficulty, problem_id=pid), authored=True)


@pytest_asyncio.fixture
async def neo_seeded(neo4j_test):
    """Neo4j seeded with a fluid_mechanics concept + alias + intro/standard problems."""
    await writer.write_concept(neo4j_test, ConceptRegistryEntry(
        subject_id="fluid_mechanics", concept_id="bernoulli_principle",
        scope_summary="b", canonical_symbols=CanonicalSymbols(symbols=["P"]),
        normalization_map={}, parser_prompt_template="P",
        solver_hints=SolverHints()), source_document_id="seed",
        scope_embedding=[0.0] * 3072, policy_frozen=True)
    await writer.write_cluster_alias(neo4j_test, "fluid_mechanics",
                                     "fluid_mechanics", "bernoulli_principle")
    await _seed_problem(neo4j_test, pid=CURRENT_PROBLEM_ID, difficulty="intro")
    await _seed_problem(neo4j_test, pid="seed_intro_p2", difficulty="intro")
    await _seed_problem(neo4j_test, pid="seed_standard_p1", difficulty="standard")
    return neo4j_test


@pytest_asyncio.fixture
async def db_with_report_session():
    """Session in REPORT phase with one graded attempt on the current problem."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        ApolloSession.__table__,
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
            current_problem_id=CURRENT_PROBLEM_ID,
        )
        s.add(sess)
        await s.flush()
        s.add(ProblemAttempt(
            session_id=sess.id,
            problem_id=CURRENT_PROBLEM_ID,
            difficulty="intro",
            result="solved",
        ))
        await s.commit()
        await s.refresh(sess)
        yield s, sess.id
    await engine.dispose()


@pytest.mark.asyncio
async def test_next_from_report_advances(db_with_report_session, neo_seeded):
    s, session_id = db_with_report_session
    result = await handle_next(db=s, neo=neo_seeded, session_id=session_id, difficulty="standard")

    assert result["session_id"] == session_id
    assert result["attempt_id"] is not None
    assert result["problem"]["id"] != CURRENT_PROBLEM_ID

    sess = (await s.execute(select(ApolloSession).where(ApolloSession.id == session_id))).scalar_one()
    assert sess.phase == SessionPhase.TEACHING.value
    assert sess.current_problem_id == result["problem"]["id"]

    prior = (await s.execute(
        select(ProblemAttempt)
        .where(ProblemAttempt.session_id == session_id)
        .where(ProblemAttempt.problem_id == CURRENT_PROBLEM_ID)
    )).scalar_one()
    assert prior.result == "solved"

    new_attempt = (await s.execute(
        select(ProblemAttempt).where(ProblemAttempt.id == result["attempt_id"])
    )).scalar_one()
    assert new_attempt.difficulty == "standard"
    assert new_attempt.result is None


@pytest.mark.asyncio
async def test_next_from_teaching_abandons_current(db_with_report_session, neo_seeded):
    s, session_id = db_with_report_session
    sess = (await s.execute(select(ApolloSession).where(ApolloSession.id == session_id))).scalar_one()
    sess.phase = SessionPhase.TEACHING.value
    prior = (await s.execute(
        select(ProblemAttempt).where(ProblemAttempt.session_id == session_id)
    )).scalar_one()
    prior.result = None
    await s.commit()

    result = await handle_next(db=s, neo=neo_seeded, session_id=session_id, difficulty="standard")

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
        await handle_next(db=s, neo=None, session_id=session_id, difficulty="standard")


@pytest.mark.asyncio
async def test_next_raises_invalid_phase_from_init(db_with_report_session):
    s, session_id = db_with_report_session
    sess = (await s.execute(select(ApolloSession).where(ApolloSession.id == session_id))).scalar_one()
    sess.phase = SessionPhase.INIT.value
    await s.commit()
    with pytest.raises(InvalidPhaseError):
        await handle_next(db=s, neo=None, session_id=session_id, difficulty="standard")


@pytest.mark.asyncio
async def test_next_raises_pool_exhausted_when_all_problems_attempted(db_with_report_session, monkeypatch):
    s, session_id = db_with_report_session
    async def _boom(*, cluster_id, difficulty, attempted_ids, neo):
        raise PoolExhaustedError(concept_cluster_id=cluster_id, difficulty=difficulty)
    monkeypatch.setattr("apollo.handlers.next.select_problem", _boom)
    with pytest.raises(PoolExhaustedError):
        await handle_next(db=s, neo=None, session_id=session_id, difficulty="hard")


@pytest.mark.asyncio
async def test_next_excludes_prior_problem_ids(db_with_report_session, neo_seeded, monkeypatch):
    s, session_id = db_with_report_session
    captured = {}
    async def _spy(*, cluster_id, difficulty, attempted_ids, neo):
        captured["attempted_ids"] = list(attempted_ids)
        from apollo.overseer.problem_selector import select_problem as real
        return await real(cluster_id=cluster_id, difficulty=difficulty,
                          attempted_ids=attempted_ids, neo=neo)
    monkeypatch.setattr("apollo.handlers.next.select_problem", _spy)
    await handle_next(db=s, neo=neo_seeded, session_id=session_id, difficulty="intro")
    assert CURRENT_PROBLEM_ID in captured["attempted_ids"]
