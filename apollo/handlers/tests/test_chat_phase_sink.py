"""`handle_chat`'s optional `on_phase` sink (study-prep B.1 Tier 1).

The streaming route is a VIEW of a turn, so the sink has to be provably inert:

* the BLOCKING route passes no sink, and its payload must stay byte-identical
  to its pre-streaming behavior (whole-payload comparison, never partial);
* P0.3 ordering is unchanged with a sink attached — the student message is
  durable before the LLM chain, and the `thinking` phase (the unified call) is
  announced only after that row is committed;
* a sink that raises must not fail the turn.

Same SQLite + boundary-mock harness as test_chat_done_race.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.conftest import TEST_SPACE_ID, TEST_USER_ID, TEST_USER_ID_2
from apollo.handlers.chat import (
    TURN_PHASE_GRADING,
    TURN_PHASE_READING,
    TURN_PHASE_REPLY,
    TURN_PHASE_THINKING,
)
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
async def db_two_sessions():
    """One SQLite DB with TWO identically-seeded sessions, so the blocking and
    sink-attached runs never contaminate each other's transcript. Distinct
    users because `learning_activities` is UNIQUE on (user_id, course_id)."""
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
        ids = []
        for user_id in (TEST_USER_ID, TEST_USER_ID_2):
            sess = TutoringSession(
                user_id=user_id,
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
            ids.append((sess.id, attempt.id))
        yield s, ids
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


def _ask_planner():
    return AsyncMock(
        return_value=QuestionDecision(action="ask", question="tell me more", target_node_id=None)
    )


async def test_blocking_payload_is_byte_identical_with_and_without_a_sink(db_two_sessions):
    """The whole-payload pin. A sink may observe a turn; it may not change one."""
    db, ids = db_two_sessions
    (blocking_sid, _), (streamed_sid, _) = ids
    from apollo.handlers.chat import handle_chat

    ps = _base_patches(_fake_store(), _ask_planner())
    with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5]:
        blocking = await handle_chat(
            db=db, neo=MagicMock(), session_id=blocking_sid, message="the same words"
        )
        streamed = await handle_chat(
            db=db,
            neo=MagicMock(),
            session_id=streamed_sid,
            message="the same words",
            on_phase=lambda _phase, _fields: None,
        )

    assert json.dumps(blocking, sort_keys=True) == json.dumps(streamed, sort_keys=True)


async def test_student_row_is_durable_before_the_thinking_phase_is_announced(db_two_sessions):
    """P0.3 under streaming: `reading` fires only after the student's message is
    committed, and `thinking` only right before the unified call — so a Done
    racing this turn always sees the message the stream is narrating."""
    db, ids = db_two_sessions
    session_id, attempt_id = ids[0]
    timeline: list[tuple[str, object]] = []

    async def _capture_planner(db_arg, **kwargs):
        rows = (
            (
                await db_arg.execute(
                    select(TutoringMessage)
                    .where(TutoringMessage.attempt_id == attempt_id)
                    .order_by(TutoringMessage.turn_index)
                )
            )
            .scalars()
            .all()
        )
        timeline.append(("planner", [(r.role, r.content) for r in rows]))
        return QuestionDecision(action="ask", question="tell me more", target_node_id=None)

    def _sink(phase: str, fields: dict) -> None:
        timeline.append((phase, fields))

    ps = _base_patches(_fake_store(), AsyncMock(side_effect=_capture_planner))
    from apollo.handlers.chat import handle_chat

    with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5]:
        await handle_chat(
            db=db,
            neo=MagicMock(),
            session_id=session_id,
            message="my best explanation",
            on_phase=_sink,
        )

    assert [entry[0] for entry in timeline] == [
        TURN_PHASE_READING,
        TURN_PHASE_THINKING,
        "planner",
        TURN_PHASE_REPLY,
    ]
    # The row the racing Done would grade is already durable at planning time.
    assert timeline[2][1] == [("student", "my best explanation")]
    assert timeline[3][1] == {"text": "tell me more"}


async def test_the_reply_row_is_durable_before_the_reply_frame_is_emitted(db_two_sessions):
    """Review wave: the frame used to go out BEFORE `_persist_apollo_reply`, so
    a commit failure left the student reading a reply no refresh could show.

    Pinned at the seam rather than by wall-clock ordering: the persist wrapper
    records how many apollo rows are committed at the moment it returns, and the
    sink records the frame — `persist` must come first, with the row already
    there. The student UI's `ApolloChat` keep-rule comment cites this ordering,
    so the citation is only true while this test is.
    """
    from apollo.handlers import chat as chat_module

    db, ids = db_two_sessions
    session_id, attempt_id = ids[0]
    timeline: list[str] = []
    real_persist = chat_module._persist_apollo_reply

    async def _tracking_persist(db_arg, **kwargs):
        await real_persist(db_arg, **kwargs)
        rows = (
            (
                await db_arg.execute(
                    select(TutoringMessage).where(
                        TutoringMessage.attempt_id == attempt_id,
                        TutoringMessage.role == "apollo",
                    )
                )
            )
            .scalars()
            .all()
        )
        timeline.append(f"persisted:{len(rows)}")

    planner = AsyncMock(
        return_value=QuestionDecision(action="ask", question="tell me more", target_node_id=None)
    )
    ps = _base_patches(_fake_store(), planner)
    from apollo.handlers.chat import handle_chat

    with (
        ps[0],
        ps[1],
        ps[2],
        ps[3],
        ps[4],
        ps[5],
        patch.object(chat_module, "_persist_apollo_reply", new=_tracking_persist),
    ):
        await handle_chat(
            db=db,
            neo=MagicMock(),
            session_id=session_id,
            message="my best explanation",
            on_phase=lambda phase, _fields: timeline.append(phase),
        )

    assert timeline == [
        TURN_PHASE_READING,
        TURN_PHASE_THINKING,
        "persisted:1",
        TURN_PHASE_REPLY,
    ]


async def test_auto_done_announces_grading_before_dispatching_handle_done(db_two_sessions):
    """The reply is released BEFORE the 6-14s grading run, then `grading` says
    why the stream is still open."""
    db, ids = db_two_sessions
    session_id, _ = ids[0]
    phases: list[str] = []

    planner = AsyncMock(
        return_value=QuestionDecision(action="done", question=None, target_node_id=None)
    )

    async def _done(**_kwargs):
        phases.append("handle_done")
        return {"grade": "B"}

    ps = _base_patches(_fake_store(), planner)
    from apollo.handlers.chat import handle_chat

    with (
        ps[0],
        ps[1],
        ps[2],
        ps[3],
        ps[4],
        ps[5],
        patch("apollo.handlers.done.handle_done", new=AsyncMock(side_effect=_done)),
    ):
        await handle_chat(
            db=db,
            neo=MagicMock(),
            session_id=session_id,
            message="that is everything",
            on_phase=lambda phase, _fields: phases.append(phase),
        )

    assert phases == [
        TURN_PHASE_READING,
        TURN_PHASE_THINKING,
        TURN_PHASE_REPLY,
        TURN_PHASE_GRADING,
        "handle_done",
    ]


async def test_a_sink_that_raises_never_fails_the_turn(db_two_sessions):
    """A progress channel is not allowed to cost a student their turn."""
    db, ids = db_two_sessions
    session_id, _ = ids[0]

    def _broken_sink(_phase: str, _fields: dict) -> None:
        raise RuntimeError("client went away in the worst possible way")

    ps = _base_patches(_fake_store(), _ask_planner())
    from apollo.handlers.chat import handle_chat

    with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5]:
        payload = await handle_chat(
            db=db,
            neo=MagicMock(),
            session_id=session_id,
            message="hello",
            on_phase=_broken_sink,
        )

    assert payload["apollo_reply"] == "tell me more"
