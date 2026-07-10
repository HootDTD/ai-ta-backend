"""Handler-level wiring tests for the clarification loop in handle_chat.

The deep clarification logic (residual nodes → flagged → hints → writes) is
fully covered by test_turn.py. Here we only verify the WIRING at the
apollo.handlers.chat boundary: the right collaborators are called with the
right args, and the fail-safe path still returns a well-formed response.

Pattern mirrors test_chat_crossturn.py: SQLite db_session_attempt fixture +
_patches/_fake_store style, with clarification collaborators additionally
mocked at the chat module boundary.
"""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.clarification.candidate_assembly import (
    load_problem_candidates,  # noqa: F401 (import check)
)
from apollo.conftest import TEST_SPACE_ID, TEST_USER_ID
from apollo.graph_compare.problem_inputs import ProblemInputs
from apollo.handlers import chat
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
from apollo.resolution.candidates import Candidate
from database.models import Base

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


@pytest_asyncio.fixture
async def db_session_only():
    """Session without a ProblemAttempt — used to test the 'no attempt' error guard."""
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
        # No ProblemAttempt added — intentional.
        yield s, sess.id
    await engine.dispose()


def _fake_store(*, nodes_added: int = 0):
    store = MagicMock()
    store.read_graph = AsyncMock(return_value=KGGraph())
    store.write_nodes = AsyncMock(return_value=nodes_added)
    store.write_edges = AsyncMock(return_value=WriteEdgesResult(written=0))
    store.summarize_for_apollo = AsyncMock(return_value="(summary)")
    return store


def _one_candidate():
    return Candidate(
        canonical_key="cond.bernoulli",
        canon_key=1,
        node_type="condition",
        is_misconception=False,
        symbolic=None,
        aliases=(),
        display_name="d",
        opposes_key=None,
        exact_aliases=(),
    )


def _fake_inputs():
    return ProblemInputs(candidates=(_one_candidate(),), symbolic_mappings={})


def _patches(store, *, mock_draft_reply=None):
    """Base patch set mirroring test_chat_crossturn + clarification collaborators."""
    dr = mock_draft_reply if mock_draft_reply is not None else MagicMock(return_value="ok")
    return [
        patch("apollo.handlers.chat.KGStore", return_value=store),
        patch("apollo.handlers.chat.parse_utterance", return_value=([], [])),
        patch("apollo.handlers.chat.draft_reply", new=dr),
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
            "apollo.handlers.chat.load_concept_definition",
            new=AsyncMock(return_value=MagicMock()),
        ),
        # Clarification collaborators
        patch(
            "apollo.handlers.chat._find_problem_payload",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "apollo.handlers.chat.load_problem_candidates",
            new=AsyncMock(return_value=_fake_inputs()),
        ),
        patch(
            "apollo.handlers.chat.run_clarification_detection",
            new=AsyncMock(
                return_value=["Make the student commit to the DIRECTION of the relationship."]
            ),
        ),
    ]


# ---------------------------------------------------------------------------
# Feature flag tests
# ---------------------------------------------------------------------------


def test_clarification_flag_name():
    assert chat._CLARIFICATION_ENABLED_FLAG == "APOLLO_CLARIFICATION_ENABLED"


def test_clarification_enabled_default_off(monkeypatch):
    monkeypatch.delenv(chat._CLARIFICATION_ENABLED_FLAG, raising=False)
    assert chat._clarification_enabled() is False


@pytest.mark.parametrize("raw", ["1", "true", "yes"])
def test_clarification_enabled_truthy(monkeypatch, raw):
    monkeypatch.setenv(chat._CLARIFICATION_ENABLED_FLAG, raw)
    assert chat._clarification_enabled() is True


@pytest.mark.parametrize("raw", ["0", "false", "", "no"])
def test_clarification_enabled_falsey(monkeypatch, raw):
    monkeypatch.setenv(chat._CLARIFICATION_ENABLED_FLAG, raw)
    assert chat._clarification_enabled() is False


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clarification_hints_reach_draft_reply(db_session_attempt, monkeypatch):
    """run_clarification_detection's hints are forwarded to draft_reply."""
    monkeypatch.setenv("APOLLO_CLARIFICATION_ENABLED", "1")
    db, session_id, attempt_id = db_session_attempt
    store = _fake_store()
    mock_draft = MagicMock(return_value="ok")
    ps = _patches(store, mock_draft_reply=mock_draft)

    # Enter all patches via nested with; guard is a passthrough so no live LLM fires.
    with (
        ps[0],
        ps[1],
        ps[2],
        ps[3],
        ps[4],
        ps[5],
        ps[6],
        ps[7],
        ps[8],
        ps[9] as mock_detect,
        patch(
            "apollo.handlers.chat.guard_clarification_reply",
            new=MagicMock(side_effect=lambda **kw: kw["draft"]),
        ),
        patch(
            "apollo.handlers.chat.resolve_pending_clarifications",
            new=AsyncMock(),
        ) as mock_resolve,
    ):
        from apollo.handlers.chat import handle_chat

        await handle_chat(db=db, neo=MagicMock(), session_id=session_id, message="hi")

    # resolve_pending_clarifications was awaited once (re-scoring prior probes)
    assert mock_resolve.await_count == 1

    # run_clarification_detection was awaited with the right candidates
    assert mock_detect.called
    call_kwargs = mock_detect.call_args.kwargs
    assert call_kwargs["candidates"] == _fake_inputs().candidates

    # draft_reply received the hints
    assert mock_draft.call_args.kwargs["clarification_hints"] == [
        "Make the student commit to the DIRECTION of the relationship."
    ]


@pytest.mark.asyncio
async def test_resolve_pending_clarifications_receives_handler_neo_client(
    db_session_attempt, monkeypatch
):
    """T7 (plan Wave 3, spec §5.5 Q3): handle_chat's own `neo` client is
    threaded into resolve_pending_clarifications so the emergent map's
    clarification-refuted seam can eagerly materialize :Canon. Verifies the
    exact object identity reaches the call, not just that some neo is
    passed."""
    monkeypatch.setenv("APOLLO_CLARIFICATION_ENABLED", "1")
    db, session_id, attempt_id = db_session_attempt
    store = _fake_store()
    mock_draft = MagicMock(return_value="ok")
    ps = _patches(store, mock_draft_reply=mock_draft)

    neo_sentinel = MagicMock(name="neo_client")

    with (
        ps[0],
        ps[1],
        ps[2],
        ps[3],
        ps[4],
        ps[5],
        ps[6],
        ps[7],
        ps[8],
        ps[9],
        patch(
            "apollo.handlers.chat.guard_clarification_reply",
            new=MagicMock(side_effect=lambda **kw: kw["draft"]),
        ),
        patch(
            "apollo.handlers.chat.resolve_pending_clarifications",
            new=AsyncMock(),
        ) as mock_resolve,
    ):
        from apollo.handlers.chat import handle_chat

        await handle_chat(db=db, neo=neo_sentinel, session_id=session_id, message="hi")

    mock_resolve.assert_awaited_once()
    assert mock_resolve.await_args.kwargs["neo"] is neo_sentinel


@pytest.mark.asyncio
async def test_clarification_guard_redrafts_on_leak(db_session_attempt, monkeypatch):
    """When guard_clarification_reply invokes regenerate_without_probes, draft_reply
    is called a second time (covering the regenerate lambda body in chat.py).
    Also verifies that guard_clarification_reply is called exactly once."""
    monkeypatch.setenv("APOLLO_CLARIFICATION_ENABLED", "1")
    db, session_id, attempt_id = db_session_attempt
    store = _fake_store()
    mock_draft = MagicMock(return_value="ok")
    ps = _patches(store, mock_draft_reply=mock_draft)

    # Guard invokes regenerate_without_probes() to simulate a confident leak → regen.
    guard_regen = MagicMock(side_effect=lambda **kw: kw["regenerate_without_probes"]())

    with (
        ps[0],
        ps[1],
        ps[2],
        ps[3],
        ps[4],
        ps[5],
        ps[6],
        ps[7],
        ps[8],
        ps[9],
        patch("apollo.handlers.chat.guard_clarification_reply", new=guard_regen),
        patch(
            "apollo.handlers.chat.resolve_pending_clarifications",
            new=AsyncMock(),
        ),
    ):
        from apollo.handlers.chat import handle_chat

        result = await handle_chat(db=db, neo=MagicMock(), session_id=session_id, message="hi")

    assert "apollo_reply" in result
    # Once for the main reply, once via the regenerate lambda
    assert mock_draft.call_count == 2
    # guard was called (the regenerate path ran)
    guard_regen.assert_called_once()


@pytest.mark.asyncio
async def test_clarification_setup_failure_is_failsafe(db_session_attempt, monkeypatch):
    """When load_problem_candidates raises, the turn still returns a reply
    and run_clarification_detection is never called."""
    monkeypatch.setenv("APOLLO_CLARIFICATION_ENABLED", "1")
    db, session_id, attempt_id = db_session_attempt
    store = _fake_store()
    mock_draft = MagicMock(return_value="ok")
    ps = _patches(store, mock_draft_reply=mock_draft)

    # Override load_problem_candidates to raise
    boom = AsyncMock(side_effect=RuntimeError("boom"))

    with (
        ps[0],
        ps[1],
        ps[2],
        ps[3],
        ps[4],
        ps[5],
        ps[6],
        ps[7],
        patch("apollo.handlers.chat.load_problem_candidates", new=boom),
        ps[9] as mock_detect,
        patch(
            "apollo.handlers.chat.resolve_pending_clarifications",
            new=AsyncMock(),
        ),
    ):
        from apollo.handlers.chat import handle_chat

        result = await handle_chat(db=db, neo=MagicMock(), session_id=session_id, message="hi")

    assert "apollo_reply" in result
    assert not mock_detect.called
    # clarification_hints falls back to None ([] or None via `clarification_hints or None`)
    assert mock_draft.call_args.kwargs["clarification_hints"] is None


@pytest.mark.asyncio
async def test_clarification_savepoint_isolates_db_fault(db_session_attempt, monkeypatch):
    """With the flag ON, a DB fault inside the clarification block is isolated by the
    savepoint: the outer transaction remains clean, db.commit() succeeds, and the turn
    returns a normal reply (no PendingRollbackError, no hints).
    guard_clarification_reply must NOT be called (hints are empty)."""
    monkeypatch.setenv("APOLLO_CLARIFICATION_ENABLED", "1")
    db, session_id, attempt_id = db_session_attempt
    store = _fake_store()
    mock_draft = MagicMock(return_value="ok")
    ps = _patches(store, mock_draft_reply=mock_draft)

    # Make resolve_pending_clarifications raise a DB-style error inside the savepoint.
    boom = AsyncMock(side_effect=RuntimeError("simulated db fault inside savepoint"))
    guard_mock = MagicMock(return_value="ok")

    with (
        ps[0],
        ps[1],
        ps[2],
        ps[3],
        ps[4],
        ps[5],
        ps[6],
        ps[7],
        ps[8],
        ps[9],
        patch("apollo.handlers.chat.resolve_pending_clarifications", new=boom),
        patch("apollo.handlers.chat.guard_clarification_reply", new=guard_mock),
    ):
        from apollo.handlers.chat import handle_chat

        # Must return normally — no PendingRollbackError from the outer commit.
        result = await handle_chat(db=db, neo=MagicMock(), session_id=session_id, message="hi")

    assert "apollo_reply" in result
    # draft_reply must have received clarification_hints=None ([] → falsy → None)
    assert mock_draft.call_args.kwargs["clarification_hints"] is None
    # guard is only called when hints are non-empty — must not be called here
    guard_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Coverage gap tests (chat.py paths not exercised by the main clarification tests)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_chat_raises_when_no_attempt(db_session_only):
    """If there is no ProblemAttempt for the session, handle_chat raises RuntimeError."""
    db, session_id = db_session_only
    with (
        patch(
            "apollo.handlers.chat.load_concept_definition",
            new=AsyncMock(return_value=MagicMock()),
        ),
        patch("apollo.handlers.chat.KGStore", return_value=_fake_store()),
    ):
        import pytest

        from apollo.handlers.chat import handle_chat

        with pytest.raises(RuntimeError, match="no current ProblemAttempt"):
            await handle_chat(db=db, neo=MagicMock(), session_id=session_id, message="hi")


@pytest.mark.asyncio
async def test_non_done_pending_intent_is_cleared(db_session_attempt, monkeypatch):
    """A non-'done' pending intent (e.g. 'restart') is cleared then teaching continues."""
    db, session_id, attempt_id = db_session_attempt
    # Set a non-done pending_intent on the session.
    await db.execute(
        sa_update(ApolloSession)
        .where(ApolloSession.id == session_id)
        .values(pending_intent="restart")
    )
    await db.commit()

    store = _fake_store()
    mock_draft = MagicMock(return_value="ok")
    ps = _patches(store, mock_draft_reply=mock_draft)

    with (
        ps[0],
        ps[1],
        ps[2],
        ps[3],
        ps[4],
        ps[5],
        ps[6],
        ps[7],
        ps[8],
        ps[9],
        patch("apollo.handlers.chat.guard_clarification_reply", new=MagicMock(return_value="ok")),
        patch("apollo.handlers.chat.resolve_pending_clarifications", new=AsyncMock()),
    ):
        from apollo.handlers.chat import handle_chat

        result = await handle_chat(db=db, neo=MagicMock(), session_id=session_id, message="hi")

    # Teaching continues and a reply is returned.
    assert "apollo_reply" in result
    # The session's pending_intent was cleared (committed to DB).
    from sqlalchemy import select as sa_select

    refreshed = (
        await db.execute(sa_select(ApolloSession).where(ApolloSession.id == session_id))
    ).scalar_one()
    assert refreshed.pending_intent is None


# ---------------------------------------------------------------------------
# Task 10 — NLI budget gate in chat.py
# ---------------------------------------------------------------------------


def test_nli_chat_node_cap_default(monkeypatch):
    """APOLLO_NLI_CHAT_MAX_NODES unset → _nli_chat_node_cap() returns 15."""
    monkeypatch.delenv("APOLLO_NLI_CHAT_MAX_NODES", raising=False)
    from apollo.handlers.chat import _nli_chat_node_cap

    assert _nli_chat_node_cap() == 15


def test_nli_chat_node_cap_from_env(monkeypatch):
    """APOLLO_NLI_CHAT_MAX_NODES=5 → _nli_chat_node_cap() returns 5."""
    monkeypatch.setenv("APOLLO_NLI_CHAT_MAX_NODES", "5")
    from apollo.handlers.chat import _nli_chat_node_cap

    assert _nli_chat_node_cap() == 5


def test_nli_chat_node_cap_malformed(monkeypatch):
    """APOLLO_NLI_CHAT_MAX_NODES=bad → ValueError caught → returns default 15."""
    monkeypatch.setenv("APOLLO_NLI_CHAT_MAX_NODES", "not_a_number")
    from apollo.handlers.chat import _nli_chat_node_cap

    assert _nli_chat_node_cap() == 15


@pytest.mark.asyncio
async def test_nli_chat_budget_degrades(db_session_attempt, monkeypatch):
    """With NLI enabled but node count > cap, run_clarification_detection receives nli_ctx=None.

    Sets APOLLO_NLI_CHAT_MAX_NODES=0 (any node exceeds it) and patches
    done_grading._nli_context to return a fake non-None context so the budget
    branch is reachable.  Asserts that the mock was called with nli_ctx=None
    (the lexical-only degraded path).
    """
    monkeypatch.setenv("APOLLO_CLARIFICATION_ENABLED", "1")
    monkeypatch.setenv("APOLLO_NLI_CHAT_MAX_NODES", "0")  # cap=0: any node exceeds it
    db, session_id, _ = db_session_attempt
    store = _fake_store()
    mock_detect = AsyncMock(return_value=[])  # no hints → guard not called

    fake_nli_ctx = MagicMock()  # simulates a non-None NLIContext

    patches = [
        patch("apollo.handlers.chat.KGStore", return_value=store),
        patch("apollo.handlers.chat.parse_utterance", return_value=([MagicMock()], [])),
        patch("apollo.handlers.chat.draft_reply", new=MagicMock(return_value="ok")),
        patch(
            "apollo.handlers.chat.classify_intent",
            return_value=IntentVerdict(intent="teaching", confidence=1.0, reason=""),
        ),
        patch(
            "apollo.handlers.chat.load_windowed_history",
            new=AsyncMock(return_value=(None, [])),
        ),
        patch(
            "apollo.handlers.chat._find_problem",
            new=AsyncMock(return_value=MagicMock(problem_text="find P2")),
        ),
        patch(
            "apollo.handlers.chat.load_concept_definition",
            new=AsyncMock(return_value=MagicMock()),
        ),
        patch("apollo.handlers.chat._find_problem_payload", new=AsyncMock(return_value={})),
        patch(
            "apollo.handlers.chat.load_problem_candidates",
            new=AsyncMock(return_value=_fake_inputs()),
        ),
        patch("apollo.handlers.chat.run_clarification_detection", new=mock_detect),
        patch("apollo.handlers.chat.resolve_pending_clarifications", new=AsyncMock()),
        patch("apollo.handlers.done_grading._nli_context", return_value=fake_nli_ctx),
    ]

    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        from apollo.handlers.chat import handle_chat

        result = await handle_chat(db=db, neo=MagicMock(), session_id=session_id, message="hi")

    assert "apollo_reply" in result
    assert mock_detect.called
    # Budget gate fired: nli_ctx degraded to None.
    assert mock_detect.call_args.kwargs["nli_ctx"] is None
