"""Done-race fix: the teaching path persists the student message up front.

Defect I3 (2026-08-07 bimodal-fix spec, P0.3): `handle_chat` used to persist
the (student, apollo) pair only at the END of the turn — after 10-17s of LLM
work — so a Done clicked mid-turn graded a transcript missing the student's
last (usually best) message. Contract under test:

* the student row is durable BEFORE `plan_next_question` runs (a concurrent
  `handle_done` reading the transcript at that moment sees it);
* a successful turn still ends with the same (student, apollo) row pair and
  consecutive turn indexes as before the fix;
* a turn that fails mid-chain keeps the student row (grading-favorable
  dangling row), instead of losing the message entirely.

Real SQLite; parser / planner / intent / problem lookup mocked at the
`apollo.handlers.chat` boundary (same harness as test_chat_crossturn).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.conftest import TEST_SPACE_ID, TEST_USER_ID
from apollo.handlers.intent import IntentVerdict
from apollo.knowledge_graph.store import WriteEdgesResult
from apollo.ontology import KGGraph
from apollo.persistence.models import (
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
    TutoringMessage,
    TutoringSession,
)
from apollo.smart_questions import QuestionDecision
from database.models import Base

pytestmark = pytest.mark.unit


@pytest_asyncio.fixture
async def db_session_attempt():
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
        sess = TutoringSession(
            user_id=TEST_USER_ID,
            search_space_id=TEST_SPACE_ID,
            concept_id=1,
            status=SessionStatus.active.value,
            phase=SessionPhase.TEACHING.value,
            current_problem_id=1,
            pending_intent=None,
        )
        s.add(sess)
        await s.commit()
        await s.refresh(sess)
        attempt = ProblemAttempt(
            session_id=sess.id,
            problem_id=1,
            difficulty="intro",
            user_id=sess.user_id,
            course_id=sess.course_id,
        )
        s.add(attempt)
        await s.commit()
        await s.refresh(attempt)
        yield s, sess.id, attempt.id
    await engine.dispose()


def _fake_store():
    store = MagicMock()
    store.read_graph = AsyncMock(return_value=KGGraph())
    store.write_nodes = AsyncMock(return_value=0)
    store.write_edges = AsyncMock(return_value=WriteEdgesResult(written=0))
    return store


def _base_patches(store, planner):
    return [
        patch("apollo.handlers.chat.KGStore", return_value=store),
        patch("apollo.handlers.chat.parse_utterance", return_value=([], [])),
        patch("apollo.handlers.chat.plan_next_question", new=planner),
        patch(
            "apollo.handlers.chat.classify_intent",
            return_value=IntentVerdict(intent="teaching", confidence=1.0, reason=""),
        ),
        patch(
            "apollo.handlers.chat._find_problem",
            new=AsyncMock(return_value=MagicMock(problem_text="find P2")),
        ),
        patch(
            "apollo.handlers.chat.load_concept_definition", new=AsyncMock(return_value=MagicMock())
        ),
    ]


async def _messages(db, attempt_id):
    rows = (
        (
            await db.execute(
                select(TutoringMessage)
                .where(TutoringMessage.attempt_id == attempt_id)
                .order_by(TutoringMessage.turn_index)
            )
        )
        .scalars()
        .all()
    )
    return [(row.role, row.content, row.turn_index) for row in rows]


@pytest.mark.asyncio
async def test_student_message_durable_before_question_planning(db_session_attempt):
    """A Done racing this turn reads the transcript while plan_next_question
    is still running — the student row must already be there."""
    db, session_id, attempt_id = db_session_attempt
    seen_at_planning_time: list[tuple[str, str, int]] = []

    async def _capture_planner(db_arg, **kwargs):
        seen_at_planning_time.extend(await _messages(db_arg, attempt_id))
        return QuestionDecision(action="ask", question="tell me more", target_node_id=None)

    planner = AsyncMock(side_effect=_capture_planner)
    ps = _base_patches(_fake_store(), planner)
    from apollo.handlers.chat import handle_chat

    with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5]:
        await handle_chat(
            db=db, neo=MagicMock(), session_id=session_id, message="my best explanation"
        )

    assert seen_at_planning_time == [("student", "my best explanation", 0)]


@pytest.mark.asyncio
async def test_successful_turn_ends_with_same_pair_shape_as_before(db_session_attempt):
    db, session_id, attempt_id = db_session_attempt
    planner = AsyncMock(
        return_value=QuestionDecision(action="ask", question="tell me more", target_node_id=None)
    )
    ps = _base_patches(_fake_store(), planner)
    from apollo.handlers.chat import handle_chat

    with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5]:
        await handle_chat(db=db, neo=MagicMock(), session_id=session_id, message="hello")
        await handle_chat(db=db, neo=MagicMock(), session_id=session_id, message="more detail")

    assert await _messages(db, attempt_id) == [
        ("student", "hello", 0),
        ("apollo", "tell me more", 1),
        ("student", "more detail", 2),
        ("apollo", "tell me more", 3),
    ]


@pytest.mark.asyncio
async def test_planner_turn_index_matches_student_row(db_session_attempt):
    """The question ledger keys off the student message's turn index — the
    early persist must hand the SAME value to plan_next_question that the
    pair-persist used to compute."""
    db, session_id, attempt_id = db_session_attempt
    planner = AsyncMock(
        return_value=QuestionDecision(action="ask", question="go on", target_node_id=None)
    )
    ps = _base_patches(_fake_store(), planner)
    from apollo.handlers.chat import handle_chat

    with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5]:
        await handle_chat(db=db, neo=MagicMock(), session_id=session_id, message="first")
        await handle_chat(db=db, neo=MagicMock(), session_id=session_id, message="second")

    first_kwargs = planner.await_args_list[0].kwargs
    second_kwargs = planner.await_args_list[1].kwargs
    assert first_kwargs["turn_index"] == 0
    assert second_kwargs["turn_index"] == 2


@pytest.mark.asyncio
async def test_engine_decided_done_grades_with_auto_done_stamp(db_session_attempt):
    """P0.4 call site: when the questioning engine (not the student) decides
    the attempt is done, the chat path grades via handle_done(auto_done=True)."""
    db, session_id, attempt_id = db_session_attempt
    planner = AsyncMock(
        return_value=QuestionDecision(action="done", question=None, target_node_id=None)
    )
    ps = _base_patches(_fake_store(), planner)
    done_mock = AsyncMock(return_value={"grade": "B"})
    done_patch = patch("apollo.handlers.done.handle_done", new=done_mock)
    from apollo.handlers.chat import handle_chat

    with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], done_patch:
        result = await handle_chat(
            db=db, neo=MagicMock(), session_id=session_id, message="that is everything I know"
        )

    done_mock.assert_awaited_once()
    kwargs = done_mock.await_args.kwargs
    assert kwargs["auto_done"] is True
    assert kwargs["session_id"] == session_id
    assert result["intent_executed"] == {"intent": "done", "result": {"grade": "B"}}


@pytest.mark.asyncio
async def test_failed_turn_keeps_student_message(db_session_attempt):
    """A mid-chain failure after the early persist keeps the student row —
    the message is never lost to the transcript again."""
    db, session_id, attempt_id = db_session_attempt
    planner = AsyncMock(side_effect=RuntimeError("planner down"))
    ps = _base_patches(_fake_store(), planner)
    from apollo.handlers.chat import handle_chat

    with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5]:
        with pytest.raises(RuntimeError):
            await handle_chat(
                db=db, neo=MagicMock(), session_id=session_id, message="my explanation"
            )

    assert await _messages(db, attempt_id) == [("student", "my explanation", 0)]
