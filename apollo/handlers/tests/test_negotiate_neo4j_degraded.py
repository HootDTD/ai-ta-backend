"""Apollo Neo4j degraded mode — challenge/paraphrase/skip (apollo/handlers/negotiate.py).

KG-native negotiation moves wrap their store mutation + `_kg_snapshot` re-read
in `KG_DEGRADED_ERRORS` and re-raise as `KGUnavailableError` (-> structured
503 `kg_unavailable`). `handle_get_trace` is Postgres-only — no change, no
test needed here (covered by the existing `test_negotiate.py` suite).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from neo4j.exceptions import ServiceUnavailable
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.conftest import TEST_SPACE_ID, TEST_USER_ID
from apollo.errors import KGUnavailableError
from apollo.handlers.negotiate import (
    ChallengeRequest,
    ParaphraseRequest,
    handle_challenge,
    handle_paraphrase,
    handle_skip,
)
from apollo.persistence.models import (
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
async def session(db: AsyncSession):
    s = TutoringSession(
        user_id=TEST_USER_ID,
        search_space_id=TEST_SPACE_ID,
        concept_id=1,
        status=SessionStatus.active.value,
        phase=SessionPhase.TEACHING.value,
    )
    db.add(s)
    await db.flush()
    a = ProblemAttempt(
        session_id=s.id,
        problem_id=1,
        difficulty="intro",
        user_id=s.user_id,
        course_id=s.course_id,
    )
    db.add(a)
    await db.commit()
    await db.refresh(s)
    await db.refresh(a)
    return s, a


@pytest.mark.asyncio
async def test_challenge_raises_kg_unavailable_on_service_unavailable(db, session):
    _, attempt = session
    with patch(
        "apollo.handlers.negotiate.KGStore.mark_node_disputed",
        new=AsyncMock(side_effect=ServiceUnavailable("aura down")),
    ):
        with pytest.raises(KGUnavailableError) as exc_info:
            await handle_challenge(
                db=db,
                neo=None,
                session_id=attempt.session_id,
                entry_id="eq1",
                body=ChallengeRequest(reason="that's not what I said"),
            )
    assert exc_info.value.stage == "challenge"


@pytest.mark.asyncio
async def test_paraphrase_raises_kg_unavailable_on_service_unavailable(db, session):
    _, attempt = session
    with patch(
        "apollo.handlers.negotiate.KGStore.paraphrase_node",
        new=AsyncMock(side_effect=ServiceUnavailable("aura down")),
    ):
        with pytest.raises(KGUnavailableError) as exc_info:
            await handle_paraphrase(
                db=db,
                neo=None,
                session_id=attempt.session_id,
                entry_id="eq1",
                body=ParaphraseRequest(surface_form="my words"),
            )
    assert exc_info.value.stage == "paraphrase"


@pytest.mark.asyncio
async def test_skip_raises_kg_unavailable_on_service_unavailable(db, session):
    _, attempt = session
    with patch(
        "apollo.handlers.negotiate.KGStore.skip_node",
        new=AsyncMock(side_effect=ServiceUnavailable("aura down")),
    ):
        with pytest.raises(KGUnavailableError) as exc_info:
            await handle_skip(
                db=db,
                neo=None,
                session_id=attempt.session_id,
                entry_id="eq1",
            )
    assert exc_info.value.stage == "skip"


@pytest.mark.asyncio
async def test_challenge_degraded_snapshot_read_also_raises_kg_unavailable(db, session):
    """A healthy mutation but a degraded `_kg_snapshot` re-read (Neo4j dies
    mid-request) is also wrapped — the plan explicitly includes the snapshot
    read in the same try/except."""
    from apollo.ontology import build_node

    _, attempt = session

    node = build_node(
        node_type="equation",
        node_id="eq1",
        attempt_id=attempt.id,
        source="parser",
        content={"symbolic": "A1*v1 - A2*v2", "label": "continuity"},
    )
    with (
        patch(
            "apollo.handlers.negotiate.KGStore.mark_node_disputed",
            new=AsyncMock(return_value=node),
        ),
        patch(
            "apollo.handlers.negotiate.KGStore.read_graph",
            new=AsyncMock(side_effect=ServiceUnavailable("aura down mid-request")),
        ),
    ):
        with pytest.raises(KGUnavailableError) as exc_info:
            await handle_challenge(
                db=db,
                neo=None,
                session_id=attempt.session_id,
                entry_id="eq1",
                body=ChallengeRequest(reason="mid-request drop"),
            )
    assert exc_info.value.stage == "challenge"
