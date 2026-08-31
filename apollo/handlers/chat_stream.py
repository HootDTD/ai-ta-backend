"""POST /apollo/sessions/{id}/chat/stream — a streaming VIEW of a teaching turn.

2026-08-23 study-prep §B.1 Tier 1. The blocking `POST .../chat` route takes
10-17s to answer and the student stares at a dead spinner for all of it. This
route runs the SAME turn machinery (`handle_chat`) and narrates it over
Server-Sent Events, so the first frame lands in well under a second.

## Why SSE and not literally-NDJSON

The plan says "chunked NDJSON, mirroring the `/api/ask/stream` shape the
student UI already parses". Those two clauses conflict: `/ask/stream`
(`server.py::post_ask_stream`) is **SSE**, not NDJSON — `text/event-stream`
with `event: <name>\\ndata: <json>\\n\\n` frames, and the student UI's reader
(`app/page.tsx`) splits on `\\n\\n` and reads the `event:` / `data:` lines.
"Reuse that reader" is the operative requirement, so this endpoint emits the
same SSE framing with the same media type and headers, and the student-ui
proxy route can be a near-copy of `app/api/ask/stream/route.ts`. As a
concession to the NDJSON framing, every `data:` line is a single-line JSON
object that ALSO carries its own `"event"` discriminator, so a consumer can
route on the payload alone.

## The turn is not the stream

The invariant that shapes this whole module: **a client disconnect must leave
turn state exactly as the blocking route would.** So the turn is not awaited
inline. It runs as a detached `asyncio.Task` that owns its own DB session and
pushes phase notifications onto an UNBOUNDED queue; the SSE generator only
drains that queue. When the client vanishes, Starlette cancels the generator —
the turn task is not in that cancel scope (a bare `asyncio.create_task` escapes
anyio's task group), is never cancelled by us, and never blocks on a queue
nobody is reading. It runs to completion and commits exactly as it would have.

That is also why the turn task must NOT borrow the request-scoped `db`:
FastAPI closes dependency-yielded sessions once the response finishes, which on
a disconnect is *while the turn is still running*. It opens its own session
through the same `get_db_session` machinery instead, so DB-08b RLS enforcement
is identical (`asyncio.create_task` copies the current context, so the
`current_request_user_id` the auth dep resolved is still visible to the
per-transaction listener).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.handlers.chat import (
    TURN_PHASE_GRADING,
    TURN_PHASE_READING,
    TURN_PHASE_REPLY,
    TURN_PHASE_THINKING,
    handle_chat,
)
from apollo.persistence.neo4j_client import Neo4jClient
from database.session import get_db_session

_LOG = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# Wire contract. The student-UI reader types against these names — changing
# one is a breaking change for `ai-ta-student-ui`.
# ----------------------------------------------------------------------

EVENT_RECEIVED = "received"
EVENT_WORKING = "working"
EVENT_REPLY = "reply"
EVENT_COMPLETE = "complete"
EVENT_ERROR = "error"

#: `working.stage` emitted by this module the instant the request is accepted,
#: before the turn task has run a single line. The other stages are the turn's
#: own phases (`apollo.handlers.chat.TURN_PHASE_*`).
STAGE_ACCEPTED = "accepted"

MEDIA_TYPE = "text/event-stream"
#: Same headers `/ask/stream` ships: no caching, and no nginx-family buffering
#: (Railway's proxy honours `X-Accel-Buffering`).
STREAM_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

_WORKING_MESSAGES = {
    STAGE_ACCEPTED: "Got it — Apollo is listening.",
    TURN_PHASE_READING: "Apollo is reading what you taught…",
    TURN_PHASE_THINKING: "Apollo is thinking it through…",
    TURN_PHASE_GRADING: "Apollo is grading what you taught…",
}

#: Accepted-frame copy for an explicit Ask Hoot turn (`ask_hoot=True`). The
#: aside lane returns before any turn phase fires, so this is the ONLY working
#: message an aside shows — and Hoot, not Apollo, answers it. An implicitly
#: classified question (typed without the button) still gets the teaching copy:
#: intent is unknown at accept time.
ASK_HOOT_ACCEPTED_MESSAGE = "Got it — Hoot is looking it up…"

#: Body served when a turn fails with something that has no registered Apollo
#: exception handler. Deliberately opaque — the detail is logged server-side,
#: never streamed to a student.
_GENERIC_ERROR_BODY = {
    "error_code": "internal_error",
    "message": "Something went wrong on Apollo's side — please try again.",
}

# Internal queue-item kinds (never on the wire).
_KIND_PHASE = "phase"
_KIND_COMPLETE = "complete"
_KIND_ERROR = "error"
_KIND_END = "end"


# ----------------------------------------------------------------------
# Detached turn session
# ----------------------------------------------------------------------

#: Opens the DB session the detached turn task owns for its whole run.
TurnSessionOpener = Callable[[], AbstractAsyncContextManager[AsyncSession]]

# `get_db_session` is FastAPI's session dependency (an async generator);
# wrapping it makes the SAME session — RLS listener and all — usable outside
# the request's dependency lifecycle.
_open_turn_session: TurnSessionOpener = asynccontextmanager(get_db_session)


def get_turn_session_opener() -> TurnSessionOpener:
    """FastAPI dependency: how the detached turn task opens its own session.

    A dependency rather than a direct import so route tests can point the turn
    at their harness engine — overriding `get_db_session` alone would not
    reach the detached task, which by design does not use the request-scoped
    session.
    """
    return _open_turn_session


# Strong refs to in-flight turns. `asyncio` keeps only weak references to
# tasks, and the generator frame that created this one is destroyed on client
# disconnect — exactly the case the turn must survive.
_BACKGROUND_TURNS: set[asyncio.Task] = set()


def _release_background_turn(task: asyncio.Task) -> None:
    """Drop the strong ref AND retrieve the task's exception.

    `set.discard` alone (the original callback) never touched the result, so a
    turn that died on a BaseException was garbage-collected with its exception
    unretrieved — asyncio's "exception was never retrieved" warning is not a log
    line anyone alerts on. This is the ONE server-side signal for the failure
    mode the `_KIND_END` terminal frame covers on the client side.
    """
    _BACKGROUND_TURNS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _LOG.error("apollo_stream_turn_task_failed", exc_info=exc)


# ----------------------------------------------------------------------
# Framing
# ----------------------------------------------------------------------


def sse_frame(event: str, data: dict[str, Any]) -> str:
    """One SSE frame. Mirrors `server.py::_sse_event`, plus the in-band
    `"event"` discriminator so the `data:` line stands alone as NDJSON."""
    payload = {"event": event, **data}
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _phase_frame(phase: str, fields: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Map a turn phase onto its wire event, or None to drop it.

    The phase vocabulary is CLOSED: a phase this module does not know about is
    dropped (a new turn phase must land here with its student-facing copy
    before it can reach a student), never forwarded with a missing message.
    """
    if phase == TURN_PHASE_REPLY:
        return EVENT_REPLY, {"apollo_reply": str(fields.get("text") or "")}
    message = _WORKING_MESSAGES.get(phase)
    if message is None:
        _LOG.warning("apollo_chat_stream_unknown_phase phase=%s", phase)
        return None
    return EVENT_WORKING, {"stage": phase, "message": message}


def _lookup_exception_handler(app: Any, exc: BaseException) -> Callable[..., Any] | None:
    """Starlette-style MRO walk over the app's registered exception handlers."""
    handlers = getattr(app, "exception_handlers", None) or {}
    for cls in type(exc).__mro__:
        handler = handlers.get(cls)
        if handler is not None:
            return handler
    return None


def _error_event(status: int, body: Any) -> dict[str, Any]:
    """`error` frame data: `{status, body, message}`.

    `message` is a TOP-LEVEL mirror of the body's own student-facing text. The
    `/ask/stream` reader the student UI is built around does
    `payload.message || '[error] Unknown error'` in its error branch, so
    without this a reader that reuses that shape renders nothing. Precedence:
    the Apollo handlers' `message`, then Starlette's `HTTPException` `detail`,
    then the generic envelope's message.
    """
    message = _GENERIC_ERROR_BODY["message"]
    if isinstance(body, dict):
        for key in ("message", "detail"):
            candidate = body.get(key)
            if isinstance(candidate, str) and candidate:
                message = candidate
                break
    return {"status": status, "body": body, "message": message}


async def _error_frame(request: Request, exc: BaseException) -> tuple[int, Any]:
    """Render a failed turn as `(status, body)` using the SAME registered
    handler the blocking route would have hit.

    Headers are long gone by the time a turn fails, so the status code has to
    travel in-band. Reusing the handler keeps `error.body` byte-identical to
    the blocking route's JSON body — the FE's existing 409 `session_frozen` /
    503 `coverage_grading_failed` handling works unchanged.
    """
    handler = _lookup_exception_handler(request.app, exc)
    if handler is None:
        # No registered handler means a genuine bug, and this route answers 200
        # + an in-band event — so neither Starlette's ERROR log nor an HTTP 5xx
        # dashboard would ever show it. Log it at ERROR here instead.
        _LOG.error(
            "apollo_chat_stream_unhandled_turn_error type=%s", type(exc).__name__, exc_info=exc
        )
        return 500, dict(_GENERIC_ERROR_BODY)
    try:
        response = handler(request, exc)
        if inspect.isawaitable(response):
            response = await response
        return int(response.status_code), json.loads(response.body)
    except Exception:  # noqa: BLE001 - a broken renderer still owes the client an event
        _LOG.warning("apollo_chat_stream_error_render_failed", exc_info=True)
        return 500, dict(_GENERIC_ERROR_BODY)


# ----------------------------------------------------------------------
# The turn task + the view over it
# ----------------------------------------------------------------------


async def _run_turn(
    *,
    queue: asyncio.Queue,
    open_session: TurnSessionOpener,
    neo: Neo4jClient | None,
    session_id: int,
    message: str,
    ask_hoot: bool,
) -> None:
    """Run one teaching turn to completion, narrating it onto `queue`.

    Detached from the request: nothing here reads the connection, and no
    outcome depends on anyone draining the queue.
    """

    def _sink(phase: str, fields: dict[str, Any]) -> None:
        # Same loop as this coroutine (handle_chat only emits from its own
        # coroutine, never from a to_thread worker), and the queue is
        # unbounded, so this can neither block nor need a thread hop.
        queue.put_nowait((_KIND_PHASE, (phase, fields)))

    try:
        async with open_session() as db:
            payload = await handle_chat(
                db=db,
                neo=neo,
                session_id=session_id,
                message=message,
                ask_hoot=ask_hoot,
                on_phase=_sink,
            )
        queue.put_nowait((_KIND_COMPLETE, payload))
    except Exception as exc:  # noqa: BLE001 - surfaced as an in-band error event
        _LOG.warning(
            "apollo_chat_stream_turn_failed session_id=%s error=%s",
            session_id,
            exc,
            exc_info=True,
        )
        queue.put_nowait((_KIND_ERROR, exc))
    finally:
        queue.put_nowait((_KIND_END, None))


async def stream_chat_turn(
    *,
    request: Request,
    neo: Neo4jClient | None,
    open_session: TurnSessionOpener,
    session_id: int,
    message: str,
    ask_hoot: bool = False,
) -> AsyncIterator[str]:
    """SSE frames for one teaching turn. See the module docstring for the
    disconnect contract; see `docs/architecture/apollo/conversation/handlers/
    chat-stream.md` for the full event contract."""
    queue: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(
        _run_turn(
            queue=queue,
            open_session=open_session,
            neo=neo,
            session_id=session_id,
            message=message,
            ask_hoot=ask_hoot,
        )
    )
    _BACKGROUND_TURNS.add(task)
    task.add_done_callback(_release_background_turn)

    # Both of these precede any awaited work, so "visible activity < 1s" is a
    # property of the framing, not of how fast the LLM chain happens to be.
    yield sse_frame(EVENT_RECEIVED, {"session_id": session_id})
    accepted_message = (
        ASK_HOOT_ACCEPTED_MESSAGE if ask_hoot else _WORKING_MESSAGES[STAGE_ACCEPTED]
    )
    yield sse_frame(EVENT_WORKING, {"stage": STAGE_ACCEPTED, "message": accepted_message})

    replied = False
    while True:
        kind, value = await queue.get()
        if kind == _KIND_PHASE:
            frame = _phase_frame(*value)
            if frame is None:
                continue
            event, data = frame
            replied = replied or event == EVENT_REPLY
            yield sse_frame(event, data)
        elif kind == _KIND_COMPLETE:
            # Contract: exactly one `reply` precedes `complete`. The short
            # lanes (intent confirmation, Ask Hoot aside, cap/apology) never
            # reach the teaching path's emit site, so synthesize theirs from
            # the payload rather than making every lane emit.
            if not replied:
                yield sse_frame(EVENT_REPLY, {"apollo_reply": str(value.get("apollo_reply") or "")})
            # `jsonable_encoder` is what FastAPI runs over the BLOCKING route's
            # return value, so running it here is what makes `complete.payload`
            # equal to that route's JSON body rather than merely similar.
            yield sse_frame(EVENT_COMPLETE, {"payload": jsonable_encoder(value)})
            return
        elif kind == _KIND_ERROR:
            status, body = await _error_frame(request, value)
            yield sse_frame(EVENT_ERROR, _error_event(status, body))
            return
        else:
            # `_KIND_END` is only ever READ when the turn task died without
            # pushing a terminal item — `_run_turn` catches `Exception`, so a
            # BaseException (worker-shutdown `CancelledError`, `SystemExit`, an
            # escaping `BaseExceptionGroup`) skips its error branch while the
            # `finally` still enqueues this sentinel. Emit the generic terminal
            # error rather than closing silently, so "exactly one terminal
            # event, always" is structurally true instead of contingent on
            # which exception hierarchy the turn died from.
            _LOG.error(
                "apollo_chat_stream_turn_vanished session_id=%s — turn task ended "
                "with no terminal event (BaseException?)",
                session_id,
            )
            yield sse_frame(EVENT_ERROR, _error_event(500, dict(_GENERIC_ERROR_BODY)))
            return
