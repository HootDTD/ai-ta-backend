"""WU-2B: `handle_chat` threads a `graph_context` built from the prior attempt
graph into `parse_utterance`, enabling cross-turn edge linking; cross-turn
node de-dup means a re-asserted prior id is not double-created.

NO Neo4j, NO live LLM. The parser (`parse_utterance`), unified question
planner, intent classifier (`classify_intent`), and problem lookup (`_find_problem`) are mocked
at the `apollo.handlers.chat` boundary. The `KGStore` is replaced with a mock
whose `read_graph`/`write_nodes`/`write_edges`/`summarize_for_apollo` are
controlled per-test. SQLite stands in for Postgres. Test attempt_ids/ids use
the NEGATIVE/UUID conventions.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.conftest import TEST_SPACE_ID, TEST_USER_ID
from apollo.handlers.intent import IntentVerdict
from apollo.knowledge_graph.store import WriteEdgesResult
from apollo.ontology import KGGraph, build_node
from apollo.parser.graph_context import GraphContext
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

# ---------------------------------------------------------------------------
# DB fixture with a session + attempt
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_session_attempt():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        execution_options={"schema_translate_map": {"app": None, "internal": None}},
    )
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


def _eq(node_id, attempt_id, label="continuity"):
    return build_node(
        node_type="equation",
        node_id=node_id,
        attempt_id=attempt_id,
        source="parser",
        content={"symbolic": "A1*v1 - A2*v2", "label": label},
    )


def _fake_store(*, prior_graph: KGGraph, nodes_added: int = 1, post_graph: KGGraph | None = None):
    """A mock KGStore: read_graph returns prior_graph first, post_graph after."""
    store = MagicMock()
    store.read_graph = AsyncMock(
        side_effect=[
            prior_graph,
            post_graph if post_graph is not None else prior_graph,
        ]
    )
    store.write_nodes = AsyncMock(return_value=nodes_added)
    store.write_edges = AsyncMock(return_value=WriteEdgesResult(written=0))
    store.summarize_for_apollo = AsyncMock(return_value="(summary)")
    return store


def _patches(store):
    """Patch the chat-module collaborators. Returns a list of patch context
    managers the test enters via ExitStack-style `with`."""
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
        # WU-3D: concept now resolves from the DB; _find_problem is async.
        patch(
            "apollo.handlers.chat._find_problem",
            new=AsyncMock(return_value=MagicMock(problem_text="find P2 in a horizontal pipe")),
        ),
        patch(
            "apollo.handlers.chat.load_concept_definition", new=AsyncMock(return_value=MagicMock())
        ),
    ]


async def _run_chat(db, session_id, *, parse_return):
    from apollo.handlers.chat import handle_chat

    # parse_utterance is the 2nd patch in _patches; configure via the returned mock.
    return await handle_chat(
        db=db,
        neo=MagicMock(),
        session_id=session_id,
        message="A1 v1 = A2 v2",
    ), parse_return


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_threads_graph_context_into_parser(db_session_attempt):
    db, session_id, attempt_id = db_session_attempt
    prior = KGGraph(nodes=[_eq("eq_prev", attempt_id, label="continuity")])
    store = _fake_store(prior_graph=prior)
    ps = _patches(store)
    with ps[0], ps[1] as mock_parse, ps[2], ps[3], ps[4], ps[5], ps[6]:
        mock_parse.return_value = ([], [])
        from apollo.handlers.chat import handle_chat

        await handle_chat(db=db, neo=MagicMock(), session_id=session_id, message="hi")

    assert mock_parse.called
    ctx = mock_parse.call_args.kwargs["graph_context"]
    assert isinstance(ctx, GraphContext)
    ids = {n.node_id: n.node_type for n in ctx.nodes}
    assert ids.get("eq_prev") == "equation"


@pytest.mark.asyncio
async def test_chat_passes_built_context_not_none(db_session_attempt):
    db, session_id, attempt_id = db_session_attempt
    prior = KGGraph(nodes=[_eq("eq_prev", attempt_id)])
    store = _fake_store(prior_graph=prior)
    ps = _patches(store)
    with ps[0], ps[1] as mock_parse, ps[2], ps[3], ps[4], ps[5], ps[6]:
        mock_parse.return_value = ([], [])
        from apollo.handlers.chat import handle_chat

        await handle_chat(db=db, neo=MagicMock(), session_id=session_id, message="hi")
    ctx = mock_parse.call_args.kwargs["graph_context"]
    assert ctx is not None
    assert isinstance(ctx, GraphContext)
    assert not ctx.is_empty()


@pytest.mark.asyncio
async def test_chat_empty_prior_graph_passes_empty_context(db_session_attempt):
    db, session_id, attempt_id = db_session_attempt
    store = _fake_store(prior_graph=KGGraph())
    ps = _patches(store)
    with ps[0], ps[1] as mock_parse, ps[2], ps[3], ps[4], ps[5], ps[6]:
        mock_parse.return_value = ([], [])
        from apollo.handlers.chat import handle_chat

        await handle_chat(db=db, neo=MagicMock(), session_id=session_id, message="hi")
    ctx = mock_parse.call_args.kwargs["graph_context"]
    assert ctx is not None
    assert isinstance(ctx, GraphContext)
    assert ctx.is_empty()


@pytest.mark.asyncio
async def test_chat_does_not_recreate_prior_node(db_session_attempt):
    """The parser re-asserts a prior id (eq_prev) plus a new node. write_nodes
    is asked to write both; the §6 store de-dup (covered by the store tests)
    skips the prior id. Here we assert the handler surfaces `kg_entries_added`
    as the store's CREATED count (which excludes the reused node)."""
    db, session_id, attempt_id = db_session_attempt
    prior = KGGraph(nodes=[_eq("eq_prev", attempt_id)])
    # parser returns the reused prior node + one genuinely new node.
    reused = _eq("eq_prev", attempt_id)
    new = _eq("stu_new000000000", attempt_id, label="bernoulli")
    # store reports 1 created (the new one) — reused not counted.
    store = _fake_store(prior_graph=prior, nodes_added=1)
    ps = _patches(store)
    with ps[0], ps[1] as mock_parse, ps[2], ps[3], ps[4], ps[5], ps[6]:
        mock_parse.return_value = ([reused, new], [])
        from apollo.handlers.chat import handle_chat

        resp = await handle_chat(db=db, neo=MagicMock(), session_id=session_id, message="hi")

    # write_nodes received both nodes (de-dup happens INSIDE the store).
    wn_kwargs = store.write_nodes.call_args.kwargs
    written_ids = {n.node_id for n in wn_kwargs["nodes"]}
    assert written_ids == {"eq_prev", "stu_new000000000"}
    # kg_entries_added reflects only the genuinely-new node.
    assert resp["kg_entries_added"] == 1


@pytest.mark.asyncio
async def test_chat_response_uses_unified_envelope(db_session_attempt):
    db, session_id, attempt_id = db_session_attempt
    store = _fake_store(prior_graph=KGGraph())
    ps = _patches(store)
    with ps[0], ps[1] as mock_parse, ps[2], ps[3], ps[4], ps[5], ps[6]:
        mock_parse.return_value = ([], [])
        from apollo.handlers.chat import handle_chat

        resp = await handle_chat(db=db, neo=MagicMock(), session_id=session_id, message="hi")
    assert set(resp.keys()) == {
        "apollo_reply",
        "kg_entries_added",
        "kg",
        "covered_topics",
        "question_target",
    }


@pytest.mark.asyncio
async def test_chat_ignores_off_flag_and_still_uses_unified_planner(db_session_attempt, caplog):
    db, session_id, _attempt_id = db_session_attempt
    store = _fake_store(prior_graph=KGGraph())
    ps = _patches(store)
    ps[4] = patch("apollo.handlers.chat._unified_questioning_enabled", return_value=False)

    with ps[0], ps[1] as mock_parse, ps[2] as planner, ps[3], ps[4], ps[5], ps[6]:
        mock_parse.return_value = ([], [])
        from apollo.handlers.chat import handle_chat

        with caplog.at_level("WARNING"):
            response = await handle_chat(
                db=db, neo=MagicMock(), session_id=session_id, message="hi"
            )

    planner.assert_awaited_once()
    assert response["apollo_reply"] == "ok i think i follow"
    assert "apollo_unified_questioning_flag_off_ignored" in caplog.text
    for gone in ("sufficiency", "misconception", "olm_invite"):
        assert gone not in response


@pytest.mark.asyncio
async def test_chat_writes_edges_after_nodes(db_session_attempt):
    db, session_id, attempt_id = db_session_attempt
    store = _fake_store(prior_graph=KGGraph())
    order: list[str] = []
    store.write_nodes = AsyncMock(side_effect=lambda **k: order.append("nodes") or 1)
    store.write_edges = AsyncMock(
        side_effect=lambda **k: order.append("edges") or WriteEdgesResult(written=0)
    )
    ps = _patches(store)
    with ps[0], ps[1] as mock_parse, ps[2], ps[3], ps[4], ps[5], ps[6]:
        mock_parse.return_value = ([], [])
        from apollo.handlers.chat import handle_chat

        await handle_chat(db=db, neo=MagicMock(), session_id=session_id, message="hi")
    assert order == ["nodes", "edges"]
