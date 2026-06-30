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

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.clarification.candidate_assembly import (
    load_problem_candidates,  # noqa: F401 (import check)
)
from apollo.conftest import TEST_SPACE_ID, TEST_USER_ID
from apollo.graph_compare.problem_inputs import ProblemInputs
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
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clarification_hints_reach_draft_reply(db_session_attempt):
    """run_clarification_detection's hints are forwarded to draft_reply."""
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
async def test_clarification_guard_redrafts_on_leak(db_session_attempt):
    """When guard_clarification_reply invokes regenerate_without_probes, draft_reply
    is called a second time (covering the regenerate lambda body in chat.py)."""
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


@pytest.mark.asyncio
async def test_clarification_setup_failure_is_failsafe(db_session_attempt):
    """When load_problem_candidates raises, the turn still returns a reply
    and run_clarification_detection is never called."""
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
