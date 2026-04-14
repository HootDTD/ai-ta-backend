from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.errors import NoMatchingConceptError
from apollo.hoot_bridge.session_init import init_session_from_hoot
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


@pytest.mark.asyncio
@patch("apollo.hoot_bridge.session_init.infer_concept_cluster")
async def test_init_session_creates_session_and_first_problem(mock_infer, db_session):
    mock_infer.return_value = "fluid_mechanics"

    result = await init_session_from_hoot(
        db=db_session,
        student_id="stu-1",
        hoot_transcript="Student asked about Bernoulli in horizontal pipes.",
    )

    assert result["session_id"] > 0
    assert result["problem"]["concept_id"] in ("bernoulli_principle", "continuity_equation", "volumetric_flow_rate")
    assert result["problem"]["target_unknown"]

    from sqlalchemy import select
    sess = (await db_session.execute(select(ApolloSession))).scalar_one()
    assert sess.status == SessionStatus.active.value
    assert sess.phase == SessionPhase.TEACHING.value
    assert sess.concept_cluster_id == "fluid_mechanics"

    pa = (await db_session.execute(select(ProblemAttempt))).scalar_one()
    assert pa.difficulty == "intro"


@pytest.mark.asyncio
@patch("apollo.hoot_bridge.session_init.infer_concept_cluster")
async def test_init_session_raises_on_no_match(mock_infer, db_session):
    mock_infer.side_effect = NoMatchingConceptError(transcript_summary="cooking")
    with pytest.raises(NoMatchingConceptError):
        await init_session_from_hoot(
            db=db_session,
            student_id="stu-1",
            hoot_transcript="How do I bake a cake?",
        )
