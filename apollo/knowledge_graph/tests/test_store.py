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
    await db_session.commit()
    await db_session.refresh(s)
    return s


@pytest.mark.asyncio
async def test_write_entries_then_read(db_session, sample_session):
    store = KGStore(db_session)
    entries = [
        {"type": "equation", "content": {"symbolic": "A1*v1 - A2*v2", "label": "Continuity"}},
        {"type": "condition", "content": {"applies_when": "density is constant", "label": "Incompressibility"}},
    ]
    added = await store.write_entries(sample_session.id, entries, source="parser")
    assert added == 2

    kg = await store.read_kg(sample_session.id)
    assert len(kg["equation"]) == 1
    assert kg["equation"][0]["symbolic"] == "A1*v1 - A2*v2"
    assert len(kg["condition"]) == 1


@pytest.mark.asyncio
async def test_read_kg_returns_all_five_types_even_when_empty(db_session, sample_session):
    store = KGStore(db_session)
    kg = await store.read_kg(sample_session.id)
    assert set(kg.keys()) == {"equation", "definition", "condition", "simplification", "variable_mapping"}
    for v in kg.values():
        assert v == []


@pytest.mark.asyncio
async def test_summarize_for_apollo_bullet_format(db_session, sample_session):
    store = KGStore(db_session)
    await store.write_entries(sample_session.id, [
        {"type": "equation", "content": {"symbolic": "A1*v1 - A2*v2", "label": "Continuity"}},
    ], source="parser")
    summary = await store.summarize_for_apollo(sample_session.id)
    assert "Continuity" in summary
    assert "A1*v1 - A2*v2" in summary


@pytest.mark.asyncio
async def test_summarize_empty_kg_returns_placeholder(db_session, sample_session):
    store = KGStore(db_session)
    summary = await store.summarize_for_apollo(sample_session.id)
    assert "hasn" in summary.lower() or "nothing" in summary.lower()
