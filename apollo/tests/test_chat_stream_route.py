"""`POST /apollo/sessions/{id}/chat/stream` at the router level (B.1 Tier 1).

Two things are pinned here that the unit tests cannot see:

* the streaming route and the BLOCKING route produce the same payload for the
  same turn — whole-payload comparison, so the stream can never quietly serve
  a different grade/reply shape than its fallback;
* the owner gate still answers with real HTTP status codes, because it runs
  before the 200 and its headers go out.

Same minimal-app harness as test_api_auth: real router, overridden DB /
Neo4j / auth.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Table
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import apollo.auth_deps as deps
from apollo.api import get_neo4j_client, register_exception_handlers
from apollo.api import router as apollo_router
from apollo.conftest import TEST_SPACE_ID, TEST_USER_ID
from apollo.errors import SessionFrozenError
from apollo.handlers.chat_stream import get_turn_session_opener
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
from auth import AuthContext
from database.models import Base
from database.session import get_db_session

pytestmark = pytest.mark.unit

# The two seeded sessions live in different courses because
# `learning_activities` is UNIQUE on (user_id, course_id) and both must belong
# to the token's user for the owner gate to pass.
_BLOCKING_COURSE = TEST_SPACE_ID
_STREAMING_COURSE = TEST_SPACE_ID + 1


def parse_sse(body: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for frame in body.split("\n\n"):
        if not frame.strip():
            continue
        lines = frame.split("\n")
        name = next(ln[len("event: ") :] for ln in lines if ln.startswith("event: "))
        data = json.loads(next(ln[len("data: ") :] for ln in lines if ln.startswith("data: ")))
        events.append((name, data))
    return events


@pytest.fixture
def app_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        execution_options={"schema_translate_map": {"app": None, "internal": None}},
    )
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async def _bootstrap() -> dict[int, int]:
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda sc: Base.metadata.create_all(
                    sc,
                    tables=[
                        cast(Table, TutoringSession.__table__),
                        cast(Table, ProblemAttempt.__table__),
                        cast(Table, TutoringMessage.__table__),
                    ],
                )
            )
        seeded: dict[int, int] = {}
        async with Session() as s:
            for course_id in (_BLOCKING_COURSE, _STREAMING_COURSE):
                sess = TutoringSession(
                    user_id=TEST_USER_ID,
                    search_space_id=course_id,
                    concept_id=1,
                    status=SessionStatus.active.value,
                    phase=SessionPhase.TEACHING.value,
                    current_problem_id=1,
                )
                s.add(sess)
                await s.commit()
                await s.refresh(sess)
                s.add(
                    ProblemAttempt(
                        session_id=sess.id,
                        problem_id=1,
                        difficulty="intro",
                        user_id=sess.user_id,
                        course_id=sess.course_id,
                    )
                )
                await s.commit()
                seeded[course_id] = cast(int, sess.id)
        return seeded

    seeded = asyncio.run(_bootstrap())

    async def _override_db():
        async with Session() as s:
            yield s

    @asynccontextmanager
    async def _turn_session():
        async with Session() as s:
            yield s

    app = FastAPI()
    app.include_router(apollo_router)
    register_exception_handlers(app)
    app.dependency_overrides[get_db_session] = _override_db
    app.dependency_overrides[get_neo4j_client] = lambda: None
    app.dependency_overrides[get_turn_session_opener] = lambda: _turn_session
    return app, seeded


@pytest.fixture
def authed(monkeypatch):
    monkeypatch.setattr(
        deps,
        "resolve_auth_context",
        lambda _request: AuthContext(user_id=TEST_USER_ID, access_token="tok"),
    )
    monkeypatch.setattr(deps, "has_membership", AsyncMock(return_value=True))
    return {"Authorization": "Bearer tok"}


def _turn_patches(decision: QuestionDecision):
    store = MagicMock()
    store.read_graph = AsyncMock(return_value=KGGraph())
    store.write_nodes = AsyncMock(return_value=0)
    store.write_edges = AsyncMock(return_value=WriteEdgesResult(written=0))
    return [
        patch("apollo.handlers.chat.KGStore", return_value=store),
        patch("apollo.handlers.chat.parse_utterance", return_value=([], [])),
        patch("apollo.handlers.chat.plan_next_question", new=AsyncMock(return_value=decision)),
        patch(
            "apollo.handlers.chat.classify_intent",
            return_value=IntentVerdict(intent="teaching", confidence=1.0, reason=""),
        ),
        patch("apollo.handlers.chat._find_problem", new=AsyncMock(return_value=MagicMock())),
        patch(
            "apollo.handlers.chat.load_concept_definition", new=AsyncMock(return_value=MagicMock())
        ),
    ]


def test_stream_route_serves_sse_with_unbuffered_headers(app_factory, authed):
    app, seeded = app_factory
    ps = _turn_patches(QuestionDecision(action="ask", question="go on", target_node_id=None))
    with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5]:
        r = TestClient(app).post(
            f"/apollo/sessions/{seeded[_STREAMING_COURSE]}/chat/stream",
            json={"message": "hello"},
            headers=authed,
        )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache"
    assert r.headers["x-accel-buffering"] == "no"
    assert [name for name, _ in parse_sse(r.text)] == [
        "received",
        "working",
        "working",
        "working",
        "reply",
        "complete",
    ]


def test_stream_final_payload_equals_the_blocking_route_payload(app_factory, authed):
    """Whole-payload comparison against the fallback route — the kill switch is
    only a kill switch if flipping it changes nothing but the transport."""
    app, seeded = app_factory
    client = TestClient(app)
    ps = _turn_patches(QuestionDecision(action="ask", question="go on", target_node_id=None))
    with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5]:
        blocking = client.post(
            f"/apollo/sessions/{seeded[_BLOCKING_COURSE]}/chat",
            json={"message": "hello"},
            headers=authed,
        )
        streamed = client.post(
            f"/apollo/sessions/{seeded[_STREAMING_COURSE]}/chat/stream",
            json={"message": "hello"},
            headers=authed,
        )

    assert blocking.status_code == 200
    complete = parse_sse(streamed.text)[-1]
    assert complete[0] == "complete"
    assert json.dumps(complete[1]["payload"], sort_keys=True) == json.dumps(
        blocking.json(), sort_keys=True
    )


def test_stream_reply_event_matches_the_payloads_reply(app_factory, authed):
    app, seeded = app_factory
    ps = _turn_patches(QuestionDecision(action="ask", question="go on", target_node_id=None))
    with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5]:
        r = TestClient(app).post(
            f"/apollo/sessions/{seeded[_STREAMING_COURSE]}/chat/stream",
            json={"message": "hello"},
            headers=authed,
        )
    events = dict(parse_sse(r.text))
    assert (
        events["reply"]["apollo_reply"] == events["complete"]["payload"]["apollo_reply"] == "go on"
    )


def test_stream_persists_the_turn_like_the_blocking_route(app_factory, authed):
    app, seeded = app_factory
    ps = _turn_patches(QuestionDecision(action="ask", question="go on", target_node_id=None))
    with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5]:
        TestClient(app).post(
            f"/apollo/sessions/{seeded[_STREAMING_COURSE]}/chat/stream",
            json={"message": "hello"},
            headers=authed,
        )

    async def _rows():
        from sqlalchemy import select

        async for db in app.dependency_overrides[get_db_session]():
            result = await db.execute(select(TutoringMessage).order_by(TutoringMessage.turn_index))
            return [(r.role, r.content, r.turn_index) for r in result.scalars().all()]
        return []

    assert asyncio.run(_rows()) == [
        ("student", "hello", 0),
        ("apollo", "go on", 1),
    ]


def test_a_turn_error_becomes_an_in_band_event_not_a_500(app_factory, authed):
    """Headers are already sent, so `SessionFrozenError` has to travel in the
    body — with the SAME status + body the blocking route would have used."""
    app, seeded = app_factory
    with patch(
        "apollo.handlers.chat_stream.handle_chat",
        new=AsyncMock(side_effect=SessionFrozenError(session_id="9")),
    ):
        r = TestClient(app).post(
            f"/apollo/sessions/{seeded[_STREAMING_COURSE]}/chat/stream",
            json={"message": "hello"},
            headers=authed,
        )
    assert r.status_code == 200
    name, data = parse_sse(r.text)[-1]
    assert name == "error"
    assert data["status"] == 409
    assert data["body"]["error_code"] == "session_frozen"


def test_stream_requires_a_token(app_factory):
    app, seeded = app_factory
    r = TestClient(app).post(
        f"/apollo/sessions/{seeded[_STREAMING_COURSE]}/chat/stream", json={"message": "hi"}
    )
    assert r.status_code == 401


def test_stream_hides_another_users_session_with_404(app_factory, monkeypatch):
    """The owner gate runs BEFORE the stream, so this is a real 404 — not a 200
    with an error frame."""
    app, seeded = app_factory
    monkeypatch.setattr(
        deps,
        "resolve_auth_context",
        lambda _request: AuthContext(
            user_id="c0000000-0000-4000-8000-00000000000f", access_token="tok"
        ),
    )
    r = TestClient(app).post(
        f"/apollo/sessions/{seeded[_STREAMING_COURSE]}/chat/stream",
        json={"message": "hi"},
        headers={"Authorization": "Bearer tok"},
    )
    assert r.status_code == 404
