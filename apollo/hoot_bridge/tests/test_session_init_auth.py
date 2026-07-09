"""Phase-1 auth retrofit: init_session_from_hoot takes user_id + search_space_id.

Decision note: the sibling test_session_init.py is module-level skip-marked
(legacy V2; imports the now-deleted KGEntry model) — it cannot be un-skipped
without a full rewrite. Per the Task 4 spec this NEW module replaces it for
the auth-scoping tests, while the legacy file is left untouched.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.conftest import TEST_SPACE_ID, TEST_USER_ID, TEST_USER_ID_2
from apollo.errors import NoMatchingConceptError
from apollo.hoot_bridge.session_init import init_session_from_hoot
from apollo.persistence.models import ApolloSession, ProblemAttempt, SessionPhase, SessionStatus
from apollo.schemas.problem import Problem, ReferenceStep
from database.models import Base

# WU-3D: the concept signal is now an int concept_id (not a cluster string); the
# curriculum readers are async DB calls. These tests stay on SQLite by mocking
# the concept-resolution + problem-selection I/O entirely (no apollo_concepts
# rows needed), and assert apollo_sessions.concept_id is populated.
_STUB_CONCEPT_ID = 7

# ---------------------------------------------------------------------------
# Stable in-memory stub returned by the patched select_problem
# ---------------------------------------------------------------------------

_STUB_PROBLEM = Problem(
    id="stub_p1",
    concept_id="stub_concept",
    difficulty="intro",
    problem_text="Stub problem text.",
    given_values={"v": 1.0},
    target_unknown="x",
    reference_solution=[
        ReferenceStep(
            step=1,
            entry_type="equation",
            id="eq1",
            content={"expression": "x = v"},
            depends_on=[],
        )
    ],
)


# ---------------------------------------------------------------------------
# Shared mock helper
# ---------------------------------------------------------------------------


def _mock_inference_and_selection(monkeypatch) -> None:
    """Patch the concept-resolution + problem-selection I/O so tests are fully
    hermetic — no OpenAI key and no apollo_concepts rows needed.

    The candidate-list read (``list_course_concepts``) and the DB problem
    selection (``select_problem_personalized``) are async; ``infer_concept_id`` is
    the sync LLM hop returning the chosen concept_id.
    """
    monkeypatch.setattr(
        "apollo.hoot_bridge.session_init.list_course_concepts",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "apollo.hoot_bridge.session_init.infer_concept_id",
        lambda **kwargs: _STUB_CONCEPT_ID,
    )
    # WU-6A3: the route now calls the flag-gated select_problem_personalized
    # (flag-OFF delegates byte-identically to select_problem); stub that entry point.
    monkeypatch.setattr(
        "apollo.hoot_bridge.session_init.select_problem_personalized",
        AsyncMock(return_value=_STUB_PROBLEM),
    )


# ---------------------------------------------------------------------------
# DB fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db():
    """In-memory SQLite session for the Apollo tables we exercise here.

    We create only the tables needed by session_init rather than the full
    Base.metadata because some tables in database/models.py use JSONB columns
    that SQLAlchemy's SQLite dialect cannot compile. SQLite's FK enforcement is
    off by default, so ApolloSession.search_space_id (→ aita_search_spaces.id)
    can hold any integer value without a matching parent row.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    apollo_tables = [
        ApolloSession.__table__,
        ProblemAttempt.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=apollo_tables)
        )
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        yield s
    await engine.dispose()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_init_session_creates_session_and_first_problem(db, monkeypatch):
    _mock_inference_and_selection(monkeypatch)

    result = await init_session_from_hoot(
        db=db,
        user_id=TEST_USER_ID,
        search_space_id=TEST_SPACE_ID,
        hoot_transcript="Student asked about Bernoulli in horizontal pipes.",
        difficulty="intro",
    )

    assert result["session_id"] > 0
    assert result["problem"]["concept_id"] == _STUB_PROBLEM.concept_id
    assert result["problem"]["target_unknown"] == _STUB_PROBLEM.target_unknown

    sess = (await db.execute(select(ApolloSession))).scalar_one()
    assert sess.user_id == TEST_USER_ID
    assert sess.search_space_id == TEST_SPACE_ID
    assert sess.status == SessionStatus.active.value
    assert sess.phase == SessionPhase.TEACHING.value
    assert sess.concept_id == _STUB_CONCEPT_ID

    pa = (await db.execute(select(ProblemAttempt))).scalar_one()
    assert pa.difficulty == "intro"


@pytest.mark.asyncio
async def test_init_session_ends_stale_active_session_for_same_user(db, monkeypatch):
    _mock_inference_and_selection(monkeypatch)

    first = await init_session_from_hoot(
        db=db,
        user_id=TEST_USER_ID,
        search_space_id=TEST_SPACE_ID,
        hoot_transcript="Student asked about Bernoulli in horizontal pipes.",
        difficulty="intro",
    )

    second = await init_session_from_hoot(
        db=db,
        user_id=TEST_USER_ID,
        search_space_id=TEST_SPACE_ID,
        hoot_transcript="Student asked about Bernoulli again after a break.",
        difficulty="intro",
    )

    assert second["session_id"] != first["session_id"]

    sessions = (await db.execute(select(ApolloSession).order_by(ApolloSession.id))).scalars().all()
    assert len(sessions) == 2
    assert sessions[0].id == first["session_id"]
    assert sessions[0].status == SessionStatus.ended.value
    assert sessions[1].id == second["session_id"]
    assert sessions[1].status == SessionStatus.active.value


@pytest.mark.asyncio
async def test_new_session_ends_only_this_users_active_session(db, monkeypatch):
    _mock_inference_and_selection(monkeypatch)

    first = await init_session_from_hoot(
        db=db,
        user_id=TEST_USER_ID,
        search_space_id=TEST_SPACE_ID,
        hoot_transcript="bernoulli",
        difficulty="intro",
    )
    other = await init_session_from_hoot(
        db=db,
        user_id=TEST_USER_ID_2,
        search_space_id=TEST_SPACE_ID,
        hoot_transcript="bernoulli",
        difficulty="intro",
    )
    # A second session for user 1 must end user 1's first session, not user 2's.
    await init_session_from_hoot(
        db=db,
        user_id=TEST_USER_ID,
        search_space_id=TEST_SPACE_ID,
        hoot_transcript="bernoulli",
        difficulty="intro",
    )
    s1 = await db.get(ApolloSession, first["session_id"])
    s2 = await db.get(ApolloSession, other["session_id"])
    assert s1.status == SessionStatus.ended.value
    assert s2.status == SessionStatus.active.value


@pytest.mark.asyncio
async def test_init_session_raises_on_no_match(db, monkeypatch):
    def _raise(**kwargs):
        raise NoMatchingConceptError(transcript_summary="cooking")

    monkeypatch.setattr(
        "apollo.hoot_bridge.session_init.list_course_concepts",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "apollo.hoot_bridge.session_init.infer_concept_id",
        _raise,
    )
    with pytest.raises(NoMatchingConceptError):
        await init_session_from_hoot(
            db=db,
            user_id=TEST_USER_ID,
            search_space_id=TEST_SPACE_ID,
            hoot_transcript="How do I bake a cake?",
            difficulty="intro",
        )


@pytest.mark.asyncio
async def test_init_session_honors_passed_difficulty(db, monkeypatch):
    _mock_inference_and_selection(monkeypatch)

    result = await init_session_from_hoot(
        db=db,
        user_id=TEST_USER_ID,
        search_space_id=TEST_SPACE_ID,
        hoot_transcript="teach me bernoulli",
        difficulty="standard",
    )
    attempt = (
        await db.execute(
            select(ProblemAttempt).where(ProblemAttempt.session_id == result["session_id"])
        )
    ).scalar_one()
    assert attempt.difficulty == "standard"


@pytest.mark.asyncio
async def test_init_session_rejects_unknown_difficulty(db, monkeypatch):
    with pytest.raises(ValueError):
        await init_session_from_hoot(
            db=db,
            user_id=TEST_USER_ID,
            search_space_id=TEST_SPACE_ID,
            hoot_transcript="teach me bernoulli",
            difficulty="impossible",
        )


@pytest.mark.asyncio
async def test_init_session_returns_attempt_id(db, monkeypatch):
    _mock_inference_and_selection(monkeypatch)

    result = await init_session_from_hoot(
        db=db,
        user_id=TEST_USER_ID,
        search_space_id=TEST_SPACE_ID,
        hoot_transcript="teach me bernoulli",
        difficulty="intro",
    )
    assert "attempt_id" in result
    attempt = (
        await db.execute(
            select(ProblemAttempt).where(ProblemAttempt.session_id == result["session_id"])
        )
    ).scalar_one()
    assert result["attempt_id"] == attempt.id
