"""Tests for KG store. Uses SQLAlchemy in-memory SQLite for isolation."""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.errors import SessionFrozenError
from apollo.knowledge_graph.store import KGStore
from apollo.persistence.models import ApolloSession, KGEntry, Message, ProblemAttempt, SessionPhase, SessionStatus
from database.models import Base


@pytest_asyncio.fixture
async def db_session():
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
        yield s
    await engine.dispose()


@pytest_asyncio.fixture
async def sample_session(db_session: AsyncSession):
    s = ApolloSession(student_id="stu-1", concept_cluster_id="fluid_mechanics",
                      status=SessionStatus.active.value, phase=SessionPhase.TEACHING.value)
    db_session.add(s)
    await db_session.flush()
    attempt = ProblemAttempt(session_id=s.id, problem_id="p1", difficulty="intro")
    db_session.add(attempt)
    await db_session.commit()
    await db_session.refresh(s)
    await db_session.refresh(attempt)
    s.attempt_id = attempt.id  # type: ignore[attr-defined]
    return s


@pytest.mark.asyncio
async def test_write_entries_then_read(db_session, sample_session):
    store = KGStore(db_session)
    entries = [
        {"type": "equation", "content": {"symbolic": "A1*v1 - A2*v2", "label": "Continuity"}},
        {"type": "condition", "content": {"applies_when": "density is constant", "label": "Incompressibility"}},
    ]
    added = await store.write_entries(attempt_id=sample_session.attempt_id, entries=entries, source="parser")
    assert added == 2

    kg = await store.read_kg(attempt_id=sample_session.attempt_id)
    assert len(kg["equation"]) == 1
    assert kg["equation"][0]["symbolic"] == "A1*v1 - A2*v2"
    assert len(kg["condition"]) == 1


@pytest.mark.asyncio
async def test_read_kg_returns_all_five_types_even_when_empty(db_session, sample_session):
    store = KGStore(db_session)
    kg = await store.read_kg(attempt_id=sample_session.attempt_id)
    assert set(kg.keys()) == {"equation", "definition", "condition", "simplification", "variable_mapping", "procedure_step"}
    for v in kg.values():
        assert v == []


@pytest.mark.asyncio
async def test_summarize_for_apollo_bullet_format(db_session, sample_session):
    store = KGStore(db_session)
    await store.write_entries(attempt_id=sample_session.attempt_id, entries=[
        {"type": "equation", "content": {"symbolic": "A1*v1 - A2*v2", "label": "Continuity"}},
    ], source="parser")
    summary = await store.summarize_for_apollo(attempt_id=sample_session.attempt_id)
    assert "Continuity" in summary
    assert "A1*v1 - A2*v2" in summary


@pytest.mark.asyncio
async def test_summarize_empty_kg_returns_placeholder(db_session, sample_session):
    store = KGStore(db_session)
    summary = await store.summarize_for_apollo(attempt_id=sample_session.attempt_id)
    assert "hasn" in summary.lower() or "nothing" in summary.lower()


@pytest.mark.asyncio
async def test_freeze_then_write_raises(db_session, sample_session):
    store = KGStore(db_session)
    await store.freeze(sample_session.id)
    with pytest.raises(SessionFrozenError):
        await store.write_entries(attempt_id=sample_session.attempt_id, entries=[
            {"type": "equation", "content": {"symbolic": "x - 1", "label": "X"}},
        ], source="parser")


@pytest.mark.asyncio
async def test_unfreeze_restores_writeable(db_session, sample_session):
    store = KGStore(db_session)
    await store.freeze(sample_session.id)
    await store.unfreeze(sample_session.id)
    added = await store.write_entries(attempt_id=sample_session.attempt_id, entries=[
        {"type": "equation", "content": {"symbolic": "x - 1", "label": "X"}},
    ], source="parser")
    assert added == 1


@pytest.mark.asyncio
async def test_write_entries_accepts_procedure_step(db_session, sample_session):
    store = KGStore(db_session)
    added = await store.write_entries(
        attempt_id=sample_session.attempt_id,
        entries=[{
            "type": "procedure_step",
            "content": {
                "order": 1,
                "action": "apply continuity to find v2",
                "uses_equations": ["continuity"],
                "purpose": "get v2 for bernoulli",
            },
        }],
        source="parser",
    )
    assert added == 1
    kg = await store.read_kg(attempt_id=sample_session.attempt_id)
    assert "procedure_step" in kg
    assert len(kg["procedure_step"]) == 1
    assert kg["procedure_step"][0]["action"] == "apply continuity to find v2"


@pytest.mark.asyncio
async def test_summarize_for_apollo_includes_procedure_steps(db_session, sample_session):
    store = KGStore(db_session)
    await store.write_entries(
        attempt_id=sample_session.attempt_id,
        entries=[{
            "type": "procedure_step",
            "content": {
                "order": 1,
                "action": "apply continuity to find v2",
                "uses_equations": ["continuity"],
                "purpose": "get v2 for bernoulli",
            },
        }],
        source="parser",
    )
    summary = await store.summarize_for_apollo(attempt_id=sample_session.attempt_id)
    assert "- procedure step 1: apply continuity to find v2" in summary


@pytest_asyncio.fixture
async def db_with_two_attempts():
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
            phase=SessionPhase.TEACHING.value,
            current_problem_id="p2",
        )
        s.add(sess)
        await s.flush()
        a = ProblemAttempt(session_id=sess.id, problem_id="p1", difficulty="intro", result="abandoned")
        b = ProblemAttempt(session_id=sess.id, problem_id="p2", difficulty="standard")
        s.add_all([a, b])
        await s.commit()
        yield s, sess.id, a.id, b.id
    await engine.dispose()


@pytest.mark.asyncio
async def test_kg_entries_are_scoped_by_attempt_id(db_with_two_attempts):
    db, session_id, attempt_a, attempt_b = db_with_two_attempts
    store = KGStore(db)

    await store.write_entries(
        attempt_id=attempt_a,
        entries=[{"type": "equation", "content": {"symbolic": "x - 1", "label": "A"}}],
        source="parser",
    )
    await store.write_entries(
        attempt_id=attempt_b,
        entries=[{"type": "equation", "content": {"symbolic": "y - 2", "label": "B"}}],
        source="parser",
    )

    kg_a = await store.read_kg(attempt_id=attempt_a)
    kg_b = await store.read_kg(attempt_id=attempt_b)

    labels_a = [e.get("label") for e in kg_a["equation"]]
    labels_b = [e.get("label") for e in kg_b["equation"]]
    assert labels_a == ["A"]
    assert labels_b == ["B"]
