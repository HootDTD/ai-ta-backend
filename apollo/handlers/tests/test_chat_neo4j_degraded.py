"""Apollo Neo4j degraded mode — `handle_chat` (apollo/handlers/chat.py).

A `KG_DEGRADED_ERRORS` failure from any `KGStore` method must degrade the
chat turn rather than 500 it: reads fall back to an empty `KGGraph`, writes
are skipped (`kg_entries_added=0`), and the conversational reply (Postgres +
OpenAI) ALWAYS proceeds. Mirrors the `_patches`/`_fake_store` conventions of
`test_chat_crossturn.py` — the `KGStore` CLASS is patched at the
`apollo.handlers.chat` boundary so `handle_chat`'s own `KGStore(db, neo)`
construction returns our fake.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from neo4j.exceptions import ServiceUnavailable
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.conftest import TEST_SPACE_ID, TEST_USER_ID
from apollo.handlers.intent import IntentVerdict
from apollo.knowledge_graph.store import WriteEdgesResult
from apollo.persistence.models import (
    ApolloSession,
    KGNegotiation,
    Message,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
)
from apollo.smart_questions import QuestionDecision
from database.models import Base

pytestmark = pytest.mark.unit


@pytest_asyncio.fixture
async def db_session_attempt():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        ApolloSession.__table__,
        ProblemAttempt.__table__,
        Message.__table__,
        KGNegotiation.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda sc: Base.metadata.create_all(sc, tables=tables))
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        sess = ApolloSession(
            user_id=TEST_USER_ID,
            search_space_id=TEST_SPACE_ID,
            concept_id=1,
            status=SessionStatus.active.value,
            phase=SessionPhase.TEACHING.value,
            current_problem_id="bernoulli_horizontal_pipe_find_p2",
            pending_intent=None,
        )
        s.add(sess)
        await s.commit()
        await s.refresh(sess)
        attempt = ProblemAttempt(
            session_id=sess.id,
            problem_id="bernoulli_horizontal_pipe_find_p2",
            difficulty="intro",
        )
        s.add(attempt)
        await s.commit()
        await s.refresh(attempt)
        yield s, sess.id, attempt.id
    await engine.dispose()


def _degraded_store(*, error: Exception, nodes_added: int = 0):
    """A mock KGStore whose read_graph/write_nodes/write_edges/
    summarize_for_apollo ALL raise `error` (a KG_DEGRADED_ERRORS member)."""
    store = MagicMock()
    store.read_graph = AsyncMock(side_effect=error)
    store.write_nodes = AsyncMock(side_effect=error)
    store.write_edges = AsyncMock(side_effect=error)
    store.summarize_for_apollo = AsyncMock(side_effect=error)
    return store


def _healthy_store(*, nodes_added: int = 1):
    from apollo.ontology import KGGraph

    store = MagicMock()
    store.read_graph = AsyncMock(return_value=KGGraph())
    store.write_nodes = AsyncMock(return_value=nodes_added)
    store.write_edges = AsyncMock(return_value=WriteEdgesResult(written=0))
    store.summarize_for_apollo = AsyncMock(return_value="- equation: A1*v1 - A2*v2")
    return store


def _patches(store):
    return [
        patch("apollo.handlers.chat.KGStore", return_value=store),
        patch("apollo.handlers.chat.parse_utterance"),
        patch(
            "apollo.handlers.chat.plan_next_question",
            new=AsyncMock(
                return_value=QuestionDecision(
                    action="ask", question="ok i think i follow", target_node_id="eq.a"
                )
            ),
        ),
        patch(
            "apollo.handlers.chat.classify_intent",
            return_value=IntentVerdict(intent="teaching", confidence=1.0, reason=""),
        ),
        patch("apollo.handlers.chat._unified_questioning_enabled", return_value=True),
        patch(
            "apollo.handlers.chat._find_problem",
            new=AsyncMock(return_value=MagicMock(problem_text="find P2 in a horizontal pipe")),
        ),
        patch(
            "apollo.handlers.chat.load_concept_definition", new=AsyncMock(return_value=MagicMock())
        ),
    ]


async def _run(store, db, session_id, *, parse_return=([], [])):
    ps = _patches(store)
    with ps[0], ps[1] as mock_parse, ps[2], ps[3], ps[4], ps[5], ps[6]:
        mock_parse.return_value = parse_return
        from apollo.handlers.chat import handle_chat

        return await handle_chat(db=db, neo=MagicMock(), session_id=session_id, message="hi")


# ---------------------------------------------------------------------------
# (1) KGStore methods raising KGUnavailableError -> degraded, normal reply.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_degrades_on_kg_unavailable_error(db_session_attempt):
    from apollo.errors import KGUnavailableError

    db, session_id, _attempt_id = db_session_attempt
    store = _degraded_store(error=KGUnavailableError(stage="read_graph", last_error="down"))

    resp = await _run(store, db, session_id)

    assert resp["apollo_reply"] == "ok i think i follow"
    assert resp["kg_entries_added"] == 0
    assert resp["kg"] == {"nodes": [], "edges": []}


@pytest.mark.asyncio
async def test_unified_chat_does_not_read_legacy_kg_summary(db_session_attempt):
    from apollo.errors import KGUnavailableError

    db, session_id, _attempt_id = db_session_attempt
    store = _degraded_store(error=KGUnavailableError(stage="read_graph", last_error="down"))

    ps = _patches(store)
    with ps[0], ps[1] as mock_parse, ps[2] as planner, ps[3], ps[4], ps[5], ps[6]:
        mock_parse.return_value = ([], [])
        from apollo.handlers.chat import handle_chat

        await handle_chat(db=db, neo=MagicMock(), session_id=session_id, message="hi")

    planner.assert_awaited_once()
    store.summarize_for_apollo.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_write_nodes_ok_write_edges_degraded(db_session_attempt):
    """write_nodes succeeds (nodes_added > 0) but write_edges then fails —
    the turn still degrades gracefully: nodes_added is still reported, the
    edge failure is logged and swallowed, never propagated."""
    from apollo.errors import KGUnavailableError
    from apollo.ontology import KGGraph

    db, session_id, _attempt_id = db_session_attempt
    store = MagicMock()
    store.read_graph = AsyncMock(return_value=KGGraph())
    store.write_nodes = AsyncMock(return_value=2)
    store.write_edges = AsyncMock(
        side_effect=KGUnavailableError(stage="write_edges", last_error="down mid-write")
    )
    store.summarize_for_apollo = AsyncMock(return_value="- equation: A1*v1 - A2*v2")

    resp = await _run(store, db, session_id)

    assert resp["kg_entries_added"] == 2
    store.write_nodes.assert_awaited_once()
    store.write_edges.assert_awaited_once()


# ---------------------------------------------------------------------------
# (2) Driver error mid-call (ServiceUnavailable) -> same degraded contract.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_degrades_on_service_unavailable(db_session_attempt):
    db, session_id, _attempt_id = db_session_attempt
    store = _degraded_store(error=ServiceUnavailable("connection reset"))

    resp = await _run(store, db, session_id)

    assert resp["apollo_reply"] == "ok i think i follow"
    assert resp["kg_entries_added"] == 0
    assert resp["kg"] == {"nodes": [], "edges": []}


# ---------------------------------------------------------------------------
# (3) Healthy store -> writes happen (regression, byte-identical behavior).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_healthy_store_writes_happen(db_session_attempt):
    db, session_id, _attempt_id = db_session_attempt
    store = _healthy_store(nodes_added=1)

    resp = await _run(store, db, session_id)

    assert resp["kg_entries_added"] == 1
    store.write_nodes.assert_awaited_once()
    store.write_edges.assert_awaited_once()


# ---------------------------------------------------------------------------
# _handle_pending_done / _maybe_intent_confirmation degraded reads.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_done_confirmation_degrades_kg_read(db_session_attempt):
    """sess.pending_intent == 'done' + affirmative reply -> handle_done runs,
    then the chat-shaped envelope's `kg` re-read degrades to empty on a
    Neo4j failure instead of 500ing the whole confirmation turn."""
    from apollo.errors import KGUnavailableError

    db, session_id, attempt_id = db_session_attempt
    sess = (
        await db.execute(
            __import__("sqlalchemy").select(ApolloSession).where(ApolloSession.id == session_id)
        )
    ).scalar_one()
    sess.pending_intent = "done"
    await db.commit()

    store = _degraded_store(error=KGUnavailableError(stage="read_graph", last_error="down"))
    ps = _patches(store)

    async def _fake_handle_done(*, db, neo, session_id):
        return {"rubric": {"overall": {"score": 1.0}}}

    with (
        ps[0],
        ps[1],
        ps[2],
        ps[3],
        ps[4],
        ps[5],
        ps[6],
        patch("apollo.handlers.done.handle_done", new=AsyncMock(side_effect=_fake_handle_done)),
    ):
        from apollo.handlers.chat import handle_chat

        resp = await handle_chat(db=db, neo=MagicMock(), session_id=session_id, message="yes")

    assert resp["kg"] == {"nodes": [], "edges": []}
    assert resp["intent_executed"]["intent"] == "done"


@pytest.mark.asyncio
async def test_intent_confirmation_degrades_kg_read(db_session_attempt):
    """A non-teaching intent above threshold -> confirmation prompt path.
    Its `kg` re-read degrades to empty on a Neo4j failure."""
    from apollo.errors import KGUnavailableError

    db, session_id, attempt_id = db_session_attempt
    store = _degraded_store(error=KGUnavailableError(stage="read_graph", last_error="down"))
    ps = list(_patches(store))
    ps[3] = patch(
        "apollo.handlers.chat.classify_intent",
        return_value=IntentVerdict(intent="restart", confidence=0.95, reason=""),
    )

    with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6]:
        from apollo.handlers.chat import handle_chat

        resp = await handle_chat(
            db=db, neo=MagicMock(), session_id=session_id, message="restart please"
        )

    assert resp["kg"] == {"nodes": [], "edges": []}
    assert "intent_pending" in resp
