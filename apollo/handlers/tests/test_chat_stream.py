"""`handlers/chat_stream` — the SSE view over a teaching turn (B.1 Tier 1).

The load-bearing test here is `test_client_disconnect_leaves_the_turn_fully_persisted`:
the stream is a VIEW, so a student closing the tab mid-turn must leave exactly
the same rows behind as the blocking route would. The rest pin the wire
contract the student UI is about to be written against.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.api import register_exception_handlers
from apollo.conftest import TEST_SPACE_ID, TEST_USER_ID
from apollo.errors import SessionFrozenError
from apollo.handlers import chat_stream as cs
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def parse_sse(chunks: list[str]) -> list[tuple[str, dict]]:
    """`[(event_name, data_object), ...]` from raw SSE text."""
    events: list[tuple[str, dict]] = []
    for frame in "".join(chunks).split("\n\n"):
        if not frame.strip():
            continue
        lines = frame.split("\n")
        name = next(ln[len("event: ") :] for ln in lines if ln.startswith("event: "))
        data = json.loads(next(ln[len("data: ") :] for ln in lines if ln.startswith("data: ")))
        events.append((name, data))
    return events


@asynccontextmanager
async def _null_session():
    yield MagicMock(name="db")


def _request_with_handlers() -> SimpleNamespace:
    app = FastAPI()
    register_exception_handlers(app)
    return SimpleNamespace(app=app)


def _stream(request, **overrides):
    kwargs = {
        "request": request,
        "neo": None,
        "open_session": _null_session,
        "session_id": 7,
        "message": "teach me",
        "ask_hoot": False,
    }
    kwargs.update(overrides)
    return cs.stream_chat_turn(**kwargs)


async def _drain(gen) -> list[str]:
    return [chunk async for chunk in gen]


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------


def test_sse_frame_carries_the_event_name_twice():
    """Once in the SSE `event:` line (for the reader the student UI already
    has) and once in the JSON body (so the `data:` line stands alone)."""
    assert cs.sse_frame("working", {"stage": "reading"}) == (
        'event: working\ndata: {"event": "working", "stage": "reading"}\n\n'
    )


def test_sse_frame_keeps_non_ascii_readable():
    frame = cs.sse_frame("working", {"message": "Apollo is thinking…"})
    assert "…" in frame


def test_turn_session_opener_hands_out_the_real_session_machinery():
    """Route tests override this dependency, so pin that the un-overridden
    version is genuinely `get_db_session` — an RLS-enforced session, not a
    second, unenforced session path."""
    from database.session import get_db_session

    assert cs.get_turn_session_opener() is cs._open_turn_session
    assert cs._open_turn_session.__wrapped__ is get_db_session


def test_unknown_turn_phase_is_dropped_not_forwarded():
    assert cs._phase_frame("teleporting", {}) is None


def test_reply_phase_with_no_text_degrades_to_an_empty_string():
    assert cs._phase_frame(TURN_PHASE_REPLY, {}) == ("reply", {"apollo_reply": ""})


# ---------------------------------------------------------------------------
# Event sequence
# ---------------------------------------------------------------------------


async def test_received_and_working_are_emitted_before_the_turn_does_anything():
    """The <1s UX bar is a property of the framing, not of LLM speed."""
    release = asyncio.Event()

    async def _turn(**_kwargs):
        await release.wait()
        return {"apollo_reply": "hi"}

    with patch.object(cs, "handle_chat", new=AsyncMock(side_effect=_turn)):
        gen = _stream(_request_with_handlers())
        first = await gen.__anext__()
        second = await gen.__anext__()
        assert parse_sse([first, second]) == [
            ("received", {"event": "received", "session_id": 7}),
            (
                "working",
                {
                    "event": "working",
                    "stage": cs.STAGE_ACCEPTED,
                    "message": cs._WORKING_MESSAGES[cs.STAGE_ACCEPTED],
                },
            ),
        ]
        release.set()
        await _drain(gen)


async def test_full_teaching_turn_event_sequence():
    payload = {"apollo_reply": "tell me more", "kg_entries_added": 0, "covered_topics": []}

    async def _turn(**kwargs):
        sink = kwargs["on_phase"]
        sink(TURN_PHASE_READING, {})
        sink(TURN_PHASE_THINKING, {})
        sink(TURN_PHASE_REPLY, {"text": "tell me more"})
        return payload

    with patch.object(cs, "handle_chat", new=AsyncMock(side_effect=_turn)):
        events = parse_sse(await _drain(_stream(_request_with_handlers())))

    assert [name for name, _ in events] == [
        "received",
        "working",
        "working",
        "working",
        "reply",
        "complete",
    ]
    assert [d["stage"] for name, d in events if name == "working"] == [
        cs.STAGE_ACCEPTED,
        TURN_PHASE_READING,
        TURN_PHASE_THINKING,
    ]
    assert events[4][1]["apollo_reply"] == "tell me more"
    assert events[5][1]["payload"] == payload


async def test_auto_done_turn_streams_the_reply_before_the_grading_stage():
    """The student reads Apollo's reply while grading is still running."""

    async def _turn(**kwargs):
        sink = kwargs["on_phase"]
        sink(TURN_PHASE_REPLY, {"text": "enough to grade"})
        sink(TURN_PHASE_GRADING, {})
        return {"apollo_reply": "enough to grade", "intent_executed": {"intent": "done"}}

    with patch.object(cs, "handle_chat", new=AsyncMock(side_effect=_turn)):
        events = parse_sse(await _drain(_stream(_request_with_handlers())))

    assert [name for name, _ in events] == [
        "received",
        "working",
        "reply",
        "working",
        "complete",
    ]
    assert events[3][1]["stage"] == TURN_PHASE_GRADING


async def test_short_lane_gets_a_synthesized_reply_before_complete():
    """Intent confirmations / Ask Hoot asides never reach the teaching path's
    emit site, but the contract still promises exactly one `reply`."""
    payload = {"apollo_reply": "Ready to grade — is that right?", "kg_entries_added": 0}

    with patch.object(cs, "handle_chat", new=AsyncMock(return_value=payload)):
        events = parse_sse(await _drain(_stream(_request_with_handlers())))

    assert [name for name, _ in events] == ["received", "working", "reply", "complete"]
    assert events[2][1]["apollo_reply"] == "Ready to grade — is that right?"


async def test_a_payload_without_a_reply_key_still_terminates_cleanly():
    with patch.object(cs, "handle_chat", new=AsyncMock(return_value={"kg_entries_added": 0})):
        events = parse_sse(await _drain(_stream(_request_with_handlers())))
    assert events[2] == ("reply", {"event": "reply", "apollo_reply": ""})


async def test_unknown_phase_never_reaches_the_wire():
    async def _turn(**kwargs):
        kwargs["on_phase"]("teleporting", {})
        return {"apollo_reply": "ok"}

    with patch.object(cs, "handle_chat", new=AsyncMock(side_effect=_turn)):
        events = parse_sse(await _drain(_stream(_request_with_handlers())))

    assert [name for name, _ in events] == ["received", "working", "reply", "complete"]


async def test_a_turn_that_dies_without_a_terminal_item_still_gets_one():
    """`_run_turn` catches `Exception`, so a BaseException (worker-shutdown
    CancelledError, SystemExit, an escaping BaseExceptionGroup) reaches only
    the `finally` and its sentinel. The stream must STILL terminate with an
    `error` — "exactly one terminal event, always" has to be structural, not
    contingent on which exception hierarchy killed the turn."""

    async def _only_end(*, queue, **_kwargs):
        queue.put_nowait((cs._KIND_END, None))

    with patch.object(cs, "_run_turn", new=_only_end):
        events = parse_sse(await _drain(_stream(_request_with_handlers())))

    assert [name for name, _ in events] == ["received", "working", "error"]
    assert events[-1][1] == {
        "event": "error",
        "status": 500,
        "body": cs._GENERIC_ERROR_BODY,
        "message": cs._GENERIC_ERROR_BODY["message"],
    }


async def test_a_base_exception_in_the_real_turn_runner_still_terminates():
    """The same guarantee through the REAL `_run_turn`, driven by an actual
    BaseException rather than a hand-built queue."""

    class _Shutdown(BaseException):
        pass

    with patch.object(cs, "handle_chat", new=AsyncMock(side_effect=_Shutdown())):
        before = set(cs._BACKGROUND_TURNS)
        gen = _stream(_request_with_handlers())
        events = parse_sse(await _drain(gen))
        task = (set(cs._BACKGROUND_TURNS) - before or {None}).pop()

    assert [name for name, _ in events] == ["received", "working", "error"]
    assert events[-1][1]["status"] == 500
    assert events[-1][1]["message"] == cs._GENERIC_ERROR_BODY["message"]
    # The BaseException is deliberately NOT swallowed by `_run_turn` — it stays
    # on the task, where the loop's own handling applies.
    if task is not None:
        with pytest.raises(_Shutdown):
            task.result()


def test_error_event_mirrors_the_body_message_at_top_level():
    data = cs._error_event(409, {"error_code": "session_frozen", "message": "frozen"})
    assert data == {
        "status": 409,
        "body": {"error_code": "session_frozen", "message": "frozen"},
        "message": "frozen",
    }


def test_error_event_falls_back_to_http_exception_detail():
    assert cs._error_event(403, {"detail": "nope"})["message"] == "nope"


def test_error_event_falls_back_to_generic_copy_for_a_textless_body():
    """A handler may return a non-object body; the reader still gets copy."""
    assert cs._error_event(500, ["boom"])["message"] == cs._GENERIC_ERROR_BODY["message"]
    assert (
        cs._error_event(500, {"error_code": "x"})["message"] == (cs._GENERIC_ERROR_BODY["message"])
    )


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


async def test_error_event_mirrors_the_blocking_route_status_and_body():
    """The FE's existing 409 `session_frozen` handling must work unchanged, so
    `error.body` is rendered by the SAME registered handler."""
    exc = SessionFrozenError(session_id="7")

    with patch.object(cs, "handle_chat", new=AsyncMock(side_effect=exc)):
        events = parse_sse(await _drain(_stream(_request_with_handlers())))

    assert [name for name, _ in events] == ["received", "working", "error"]
    assert events[2][1]["status"] == 409
    assert events[2][1]["body"] == {
        "error_code": "session_frozen",
        "message": "Session '7' is frozen; writes rejected",
        "session_id": "7",
    }
    # Top-level mirror, so an /ask/stream-shaped reader renders real copy.
    assert events[2][1]["message"] == "Session '7' is frozen; writes rejected"


async def test_http_exceptions_raised_inside_a_turn_keep_their_status():
    with patch.object(
        cs, "handle_chat", new=AsyncMock(side_effect=HTTPException(status_code=403, detail="nope"))
    ):
        events = parse_sse(await _drain(_stream(_request_with_handlers())))

    assert events[-1][1]["status"] == 403
    assert events[-1][1]["body"] == {"detail": "nope"}


async def test_an_unregistered_exception_is_never_leaked_to_the_student():
    with patch.object(
        cs, "handle_chat", new=AsyncMock(side_effect=RuntimeError("SUPABASE_DB_URL=postgres://…"))
    ):
        events = parse_sse(await _drain(_stream(_request_with_handlers())))

    assert events[-1][1]["status"] == 500
    assert events[-1][1]["body"] == cs._GENERIC_ERROR_BODY


async def test_a_broken_exception_handler_still_produces_an_error_event():
    app = FastAPI()

    async def _explode(_request, _exc):
        raise ValueError("handler is broken too")

    app.add_exception_handler(SessionFrozenError, _explode)
    with patch.object(
        cs, "handle_chat", new=AsyncMock(side_effect=SessionFrozenError(session_id="7"))
    ):
        events = parse_sse(await _drain(_stream(SimpleNamespace(app=app))))

    assert events[-1][1] == {
        "event": "error",
        "status": 500,
        "body": cs._GENERIC_ERROR_BODY,
        "message": cs._GENERIC_ERROR_BODY["message"],
    }


async def test_a_failure_opening_the_turn_session_becomes_an_error_event():
    @asynccontextmanager
    async def _dead_pool():
        raise RuntimeError("pool exhausted")
        yield  # pragma: no cover - unreachable, keeps this an async generator

    events = parse_sse(await _drain(_stream(_request_with_handlers(), open_session=_dead_pool)))
    assert events[-1][0] == "error"
    assert events[-1][1]["status"] == 500


# ---------------------------------------------------------------------------
# The disconnect invariant
# ---------------------------------------------------------------------------


async def test_the_turn_task_is_strongly_referenced_while_it_runs():
    """asyncio only weak-refs tasks; the frame that created this one dies on
    disconnect, which is precisely when the turn must survive."""
    release = asyncio.Event()

    async def _turn(**_kwargs):
        await release.wait()
        return {"apollo_reply": "hi"}

    before = set(cs._BACKGROUND_TURNS)
    with patch.object(cs, "handle_chat", new=AsyncMock(side_effect=_turn)):
        gen = _stream(_request_with_handlers())
        await gen.__anext__()
        spawned = set(cs._BACKGROUND_TURNS) - before
        assert len(spawned) == 1
        release.set()
        await _drain(gen)
        await asyncio.sleep(0)

    assert set(cs._BACKGROUND_TURNS) - before == set()


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
        yield Session, s, sess.id, attempt.id
    await engine.dispose()


async def test_client_disconnect_leaves_the_turn_fully_persisted(db_session_attempt):
    """THE invariant. Real `handle_chat`, real SQLite, LLM boundaries mocked:
    the client hangs up after two frames, and the turn still lands the
    (student, apollo) pair exactly as the blocking route would."""
    Session, db, session_id, attempt_id = db_session_attempt

    @asynccontextmanager
    async def _turn_session():
        async with Session() as s:
            yield s

    planner_reached = asyncio.Event()
    release_planner = asyncio.Event()

    async def _slow_planner(_db, **_kwargs):
        planner_reached.set()
        await release_planner.wait()
        return QuestionDecision(action="ask", question="tell me more", target_node_id=None)

    store = MagicMock()
    store.read_graph = AsyncMock(return_value=KGGraph())
    store.write_nodes = AsyncMock(return_value=0)
    store.write_edges = AsyncMock(return_value=WriteEdgesResult(written=0))

    with (
        patch("apollo.handlers.chat.KGStore", return_value=store),
        patch("apollo.handlers.chat.parse_utterance", return_value=([], [])),
        patch("apollo.handlers.chat.plan_next_question", new=AsyncMock(side_effect=_slow_planner)),
        patch(
            "apollo.handlers.chat.classify_intent",
            return_value=IntentVerdict(intent="teaching", confidence=1.0, reason=""),
        ),
        patch("apollo.handlers.chat._find_problem", new=AsyncMock(return_value=MagicMock())),
        patch(
            "apollo.handlers.chat.load_concept_definition", new=AsyncMock(return_value=MagicMock())
        ),
    ):
        before = set(cs._BACKGROUND_TURNS)
        gen = _stream(
            _request_with_handlers(),
            open_session=_turn_session,
            session_id=session_id,
            message="my best explanation",
        )
        await gen.__anext__()
        await gen.__anext__()
        task = (set(cs._BACKGROUND_TURNS) - before).pop()

        # ... the student closes the tab while the unified call is in flight.
        await asyncio.wait_for(planner_reached.wait(), timeout=5)
        await gen.aclose()

        release_planner.set()
        await asyncio.wait_for(task, timeout=5)

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
    assert [(r.role, r.content, r.turn_index) for r in rows] == [
        ("student", "my best explanation", 0),
        ("apollo", "tell me more", 1),
    ]
    assert task.exception() is None
