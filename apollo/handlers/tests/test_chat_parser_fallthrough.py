"""ParserCouldNotExtractError must never surface to the student.

A teaching turn the parser cannot structure (zero extractions from a
non-trivial utterance) is still a conversational turn: `handle_chat`
swallows `ParserCouldNotExtractError`, contributes zero KG entries, logs
one aggregate INFO line, and proceeds to the normal Apollo reply. Before
2026-07-16 the error propagated to an HTTP 422 the student UI rendered as
an "I didn't understand that" error card — a dead end outside the Apollo
Q&A contract. Fixture/patch conventions mirror
`test_chat_neo4j_degraded.py`.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.conftest import TEST_SPACE_ID, TEST_USER_ID
from apollo.errors import ParserCouldNotExtractError
from apollo.handlers.intent import IntentVerdict
from apollo.knowledge_graph.store import WriteEdgesResult
from apollo.ontology import KGGraph
from apollo.persistence.models import (
    ApolloSession,
    KGNegotiation,
    Message,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
)
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


def _healthy_store():
    store = MagicMock()
    store.read_graph = AsyncMock(return_value=KGGraph())
    store.write_nodes = AsyncMock(return_value=0)
    store.write_edges = AsyncMock(return_value=WriteEdgesResult(written=0))
    store.summarize_for_apollo = AsyncMock(return_value="- equation: A1*v1 - A2*v2")
    return store


def _patches(store):
    return [
        patch("apollo.handlers.chat.KGStore", return_value=store),
        patch("apollo.handlers.chat.parse_utterance"),
        patch("apollo.handlers.chat.draft_reply", return_value="ok i think i follow"),
        patch(
            "apollo.handlers.chat.classify_intent",
            return_value=IntentVerdict(intent="teaching", confidence=1.0, reason=""),
        ),
        patch("apollo.handlers.chat.load_windowed_history", new=AsyncMock(return_value=(None, []))),
        patch(
            "apollo.handlers.chat._find_problem",
            new=AsyncMock(return_value=MagicMock(problem_text="find P2 in a horizontal pipe")),
        ),
        patch(
            "apollo.handlers.chat.load_concept_definition", new=AsyncMock(return_value=MagicMock())
        ),
    ]


@pytest.mark.asyncio
async def test_parser_could_not_extract_falls_through_to_normal_reply(db_session_attempt, caplog):
    db, session_id, attempt_id = db_session_attempt
    store = _healthy_store()
    ps = _patches(store)
    with ps[0], ps[1] as mock_parse, ps[2], ps[3], ps[4], ps[5], ps[6]:
        mock_parse.side_effect = ParserCouldNotExtractError(utterance="huh what")
        from apollo.handlers.chat import handle_chat

        with caplog.at_level("INFO"):
            resp = await handle_chat(
                db=db, neo=MagicMock(), session_id=session_id, message="huh what"
            )

    assert resp["apollo_reply"] == "ok i think i follow"
    assert resp["kg_entries_added"] == 0
    assert store.write_nodes.call_args.kwargs["nodes"] == []
    assert store.write_edges.call_args.kwargs["edges"] == []
    assert (
        f"apollo_parser_no_extract_fallthrough attempt_id={attempt_id} message_len=8" in caplog.text
    )
    assert "huh what" not in caplog.text
