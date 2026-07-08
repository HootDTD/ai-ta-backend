"""T15: flag-OFF byte-identity golden (integration spec §11.5 / §8.2 / §13).

Asserts `handle_chat`'s full returned artifact (`apollo_reply`,
`kg_entries_added`, `kg`) is byte-identical to the all-flags-OFF baseline
under TWO rows, matching the §8.2 behavior matrix and the H1 fix:

1. All three flags OFF (the trivial baseline).
2. `APOLLO_CLARIFICATION_ENABLED=ON, APOLLO_RESOLVER_V2=ON,
   APOLLO_CLARIFICATION_V2_RANKER=OFF` -- a REAL rollout state (V2 grading
   rolls out separately from the clarification ranker), which spec §1.3/§8.2
   calls out by name as a strict no-op for this feature: no background
   incremental job, no session-state mutation, no NLI budget spend.

This is stronger than `test_chat_incremental_v2.test_gate_off_no_background_kick`
(which only asserts the kick/holder side effects): here we diff the actual
chat artifact dict returned to the caller for full byte-identity, run against
independent DB sessions per row so no cross-row state leaks.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

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

_FLAG_CLAR = "APOLLO_CLARIFICATION_ENABLED"
_FLAG_V2 = "APOLLO_RESOLVER_V2"
_FLAG_RANKER = "APOLLO_CLARIFICATION_V2_RANKER"

# The two OFF rows the T15 golden must assert byte-identical (spec §8.2/§11.5):
# row 1 = trivial all-OFF baseline; row 2 = the H1 matrix row.
_OFF_ROWS: tuple[tuple[str, str, str], ...] = (
    ("0", "0", "0"),
    ("1", "1", "0"),  # APOLLO_RESOLVER_V2=ON, APOLLO_CLARIFICATION_V2_RANKER=OFF (H1)
)


@pytest.fixture(autouse=True)
def _clean_incremental_holder():
    """Every test gets a fresh process-local holder + task set (module
    globals persist across tests otherwise)."""
    chat._INCREMENTAL_HOLDER = chat._IncrementalHolder()
    chat._INCREMENTAL_BACKGROUND_TASKS = set()
    yield
    chat._INCREMENTAL_HOLDER = chat._IncrementalHolder()
    chat._INCREMENTAL_BACKGROUND_TASKS = set()


async def _new_db_session_attempt():
    """Independent DB (in-memory sqlite) + session/attempt per golden row, so
    no state (turn_index, committed Messages, ...) leaks across rows."""
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
    s = Session()
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
    return s, sess.id, attempt.id, engine


def _fake_store():
    store = MagicMock()
    store.read_graph = AsyncMock(return_value=KGGraph())
    store.write_nodes = AsyncMock(return_value=0)
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


def _base_patches(store):
    """Every collaborator is a deterministic fake (fixed return value, no
    randomness/clock/uuid) so any diff between rows can only come from the
    V2-ranker gate itself, never incidental non-determinism."""
    return [
        patch("apollo.handlers.chat.KGStore", return_value=store),
        patch("apollo.handlers.chat.parse_utterance", return_value=([], [])),
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
            new=AsyncMock(return_value=MagicMock(problem_text="find P2 in a horizontal pipe")),
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
        patch(
            "apollo.handlers.chat.run_clarification_detection",
            new=AsyncMock(return_value=[]),
        ),
        patch("apollo.handlers.chat.resolve_pending_clarifications", new=AsyncMock()),
        patch(
            "apollo.handlers.chat.guard_clarification_reply", new=MagicMock(return_value="ok")
        ),
    ]


async def _run_handle_chat(db, session_id, patches):
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patches[6],
        patches[7],
        patches[8],
        patches[9],
        patches[10],
        patches[11],
    ):
        return await chat.handle_chat(db=db, neo=MagicMock(), session_id=session_id, message="hi")


async def _run_row(monkeypatch, clar: str, v2: str, ranker: str) -> dict:
    monkeypatch.setenv(_FLAG_CLAR, clar)
    monkeypatch.setenv(_FLAG_V2, v2)
    monkeypatch.setenv(_FLAG_RANKER, ranker)

    db, session_id, _attempt_id, engine = await _new_db_session_attempt()
    try:
        store = _fake_store()
        patches = _base_patches(store)
        with patch("apollo.handlers.chat._maybe_kick_incremental_v2") as mock_kick:
            result = await _run_handle_chat(db, session_id, patches)
        # H1: neither OFF row may ever kick the background job or touch the holder.
        mock_kick.assert_not_called()
        assert chat._INCREMENTAL_HOLDER._entries == {}
        assert chat._INCREMENTAL_BACKGROUND_TASKS == set()
        return result
    finally:
        await db.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_off_rows_are_byte_identical_to_baseline(monkeypatch):
    """T15 golden: the chat artifact (`apollo_reply`, `kg_entries_added`,
    `kg`) returned for the all-OFF baseline and the H1 matrix row
    (`RESOLVER_V2=ON, RANKER=OFF`) are byte-identical."""
    results = []
    for clar, v2, ranker in _OFF_ROWS:
        results.append(await _run_row(monkeypatch, clar, v2, ranker))

    baseline = results[0]
    assert baseline["apollo_reply"] == "ok"
    for row, result in zip(_OFF_ROWS, results):
        assert result == baseline, f"row {row} diverged from the all-OFF baseline: {result!r}"


@pytest.mark.asyncio
@pytest.mark.parametrize("clar,v2,ranker", list(_OFF_ROWS))
async def test_off_row_no_background_side_effects(monkeypatch, clar, v2, ranker):
    """Each OFF row individually: no background job, no session mutation
    (holder stays empty), no NLI budget spend -- restated per-row (in
    addition to the pairwise byte-identity check above) so a future CI
    failure names the exact offending row."""
    await _run_row(monkeypatch, clar, v2, ranker)
