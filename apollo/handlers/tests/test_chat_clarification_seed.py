"""Tests for T12: feedback seeding wiring (confirmed clarifications -> V2
seed), integration spec §2.2c / §6.

Acceptance criteria under test:

* A `confirmed` outcome (only, when the ranker is fully active) seeds the V2
  incremental state: running_node_max[key] == 1.0, node_source == "clarification",
  key in seeded_keys.
* `refuted` / `vague` outcomes are never seeded.
* Seeding never fires when `_v2_ranker_active()` is False (H1 -- no session
  state mutation at all when the gate is OFF).
* Seeding a hub node does NOT resolve/lift any OTHER (neighbor) key (M5).
* The seed path is explicitly gated on `outcome == "confirmed"` (assert in
  `_confirmed_seed_keys`).
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


@pytest.fixture(autouse=True)
def _clean_incremental_holder():
    chat._INCREMENTAL_HOLDER = chat._IncrementalHolder()
    chat._INCREMENTAL_BACKGROUND_TASKS = set()
    yield
    chat._INCREMENTAL_HOLDER = chat._IncrementalHolder()
    chat._INCREMENTAL_BACKGROUND_TASKS = set()


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


def _base_patches(store, *, resolve_return=(), mock_kick=None):
    kick = mock_kick if mock_kick is not None else AsyncMock(return_value=None)
    return [
        patch("apollo.handlers.chat.KGStore", return_value=store),
        patch("apollo.handlers.chat.parse_utterance", return_value=([], [])),
        patch("apollo.handlers.chat.draft_reply", return_value="ok"),
        patch(
            "apollo.handlers.chat.classify_intent",
            return_value=IntentVerdict(intent="teaching", confidence=1.0, reason=""),
        ),
        patch(
            "apollo.handlers.chat.load_windowed_history", new=AsyncMock(return_value=(None, []))
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
        patch("apollo.handlers.chat.run_clarification_detection", new=AsyncMock(return_value=[])),
        patch(
            "apollo.handlers.chat.resolve_pending_clarifications",
            new=AsyncMock(return_value=resolve_return),
        ),
        patch("apollo.handlers.chat.guard_clarification_reply", new=MagicMock(return_value="ok")),
        patch("apollo.handlers.chat._maybe_kick_incremental_v2", new=kick),
    ]


async def _run_handle_chat(db, session_id, patches):
    from apollo.handlers.chat import handle_chat

    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[
        6
    ], patches[7], patches[8], patches[9], patches[10], patches[11], patches[12]:
        return await handle_chat(db=db, neo=MagicMock(), session_id=session_id, message="hi")


# ---------------------------------------------------------------------------
# _confirmed_seed_keys: pure gating helper
# ---------------------------------------------------------------------------


def test_confirmed_seed_keys_filters_to_confirmed_only():
    outcomes = (
        ("cond.a", "confirmed"),
        ("cond.b", "refuted"),
        ("cond.c", "vague"),
        ("cond.d", "confirmed"),
    )
    assert chat._confirmed_seed_keys(outcomes) == ("cond.a", "cond.d")


def test_confirmed_seed_keys_empty_on_no_confirmations():
    outcomes = (("cond.a", "refuted"), ("cond.b", "vague"))
    assert chat._confirmed_seed_keys(outcomes) == ()


def test_confirmed_seed_keys_empty_input():
    assert chat._confirmed_seed_keys(()) == ()


# ---------------------------------------------------------------------------
# _IncrementalHolder.seed_state
# ---------------------------------------------------------------------------


def test_seed_state_freezes_key_from_cold_holder():
    holder = chat._IncrementalHolder()
    holder.seed_state(1, ("cond.bernoulli",))
    state = holder.latest_state(1)
    assert state is not None
    assert state.running_node_max["cond.bernoulli"] == 1.0
    assert state.node_source["cond.bernoulli"] == "clarification"
    assert "cond.bernoulli" in state.seeded_keys


def test_seed_state_noop_on_empty_keys():
    holder = chat._IncrementalHolder()
    holder.seed_state(1, ())
    assert holder.latest_state(1) is None


def test_seed_state_preserves_snapshot_and_other_running_state():
    holder = chat._IncrementalHolder()
    prior_state = chat._empty_incremental_state().__class__(
        window_cursor=3,
        global_window_count=3,
        running_node_max={"cond.other": 0.6},
        node_source={"cond.other": "nli"},
        running_edge_evidence={},
        seeded_keys=frozenset(),
        pair_count_total=5,
    )
    fake_snapshot = MagicMock()
    holder.write(1, prior_state, fake_snapshot)

    holder.seed_state(1, ("cond.bernoulli",))

    state = holder.latest_state(1)
    # The hub gets frozen ...
    assert state.running_node_max["cond.bernoulli"] == 1.0
    # ... but the OTHER node's running credit is untouched (M5: no auto-lift
    # of any other key just because a hub was seeded).
    assert state.running_node_max["cond.other"] == 0.6
    assert state.node_source["cond.other"] == "nli"
    # cursor/pair budget carried through unchanged (only the seeded fields move).
    assert state.window_cursor == 3
    assert state.pair_count_total == 5
    # snapshot untouched by seeding.
    assert holder.latest_snapshot(1) is fake_snapshot


# ---------------------------------------------------------------------------
# handle_chat wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirmed_outcome_seeds_when_ranker_active(db_session_attempt, monkeypatch):
    monkeypatch.setenv(_FLAG_CLAR, "1")
    monkeypatch.setenv(_FLAG_V2, "1")
    monkeypatch.setenv(_FLAG_RANKER, "1")
    db, session_id, attempt_id = db_session_attempt
    store = _fake_store()
    patches = _base_patches(store, resolve_return=(("cond.bernoulli", "confirmed"),))

    result = await _run_handle_chat(db, session_id, patches)

    assert "apollo_reply" in result
    state = chat._INCREMENTAL_HOLDER.latest_state(attempt_id)
    assert state is not None
    assert state.running_node_max["cond.bernoulli"] == 1.0
    assert state.node_source["cond.bernoulli"] == "clarification"
    assert "cond.bernoulli" in state.seeded_keys


@pytest.mark.asyncio
async def test_refuted_outcome_does_not_seed(db_session_attempt, monkeypatch):
    monkeypatch.setenv(_FLAG_CLAR, "1")
    monkeypatch.setenv(_FLAG_V2, "1")
    monkeypatch.setenv(_FLAG_RANKER, "1")
    db, session_id, attempt_id = db_session_attempt
    store = _fake_store()
    patches = _base_patches(store, resolve_return=(("cond.bernoulli", "refuted"),))

    result = await _run_handle_chat(db, session_id, patches)

    assert "apollo_reply" in result
    assert chat._INCREMENTAL_HOLDER.latest_state(attempt_id) is None


@pytest.mark.asyncio
async def test_vague_outcome_does_not_seed(db_session_attempt, monkeypatch):
    monkeypatch.setenv(_FLAG_CLAR, "1")
    monkeypatch.setenv(_FLAG_V2, "1")
    monkeypatch.setenv(_FLAG_RANKER, "1")
    db, session_id, attempt_id = db_session_attempt
    store = _fake_store()
    patches = _base_patches(store, resolve_return=(("cond.bernoulli", "vague"),))

    result = await _run_handle_chat(db, session_id, patches)

    assert "apollo_reply" in result
    assert chat._INCREMENTAL_HOLDER.latest_state(attempt_id) is None


@pytest.mark.asyncio
async def test_confirmed_outcome_never_seeds_when_ranker_gate_off(db_session_attempt, monkeypatch):
    """H1: even a `confirmed` outcome must not mutate session state when the
    ranker isn't fully active (RESOLVER_V2 ON but RANKER OFF -- the H1 row)."""
    monkeypatch.setenv(_FLAG_CLAR, "1")
    monkeypatch.setenv(_FLAG_V2, "1")
    monkeypatch.setenv(_FLAG_RANKER, "0")
    db, session_id, attempt_id = db_session_attempt
    store = _fake_store()
    patches = _base_patches(store, resolve_return=(("cond.bernoulli", "confirmed"),))

    result = await _run_handle_chat(db, session_id, patches)

    assert "apollo_reply" in result
    assert chat._INCREMENTAL_HOLDER._entries == {}


@pytest.mark.asyncio
async def test_seeded_hub_does_not_lift_neighbor_key(db_session_attempt, monkeypatch):
    """M5: confirming a hub freezes ONLY that node -- a gray neighbor key
    already present in the running state must be left exactly as-is (not
    auto-resolved / pulled up as a side effect of the seed)."""
    monkeypatch.setenv(_FLAG_CLAR, "1")
    monkeypatch.setenv(_FLAG_V2, "1")
    monkeypatch.setenv(_FLAG_RANKER, "1")
    db, session_id, attempt_id = db_session_attempt
    store = _fake_store()

    # Prime a prior turn's state with a gray neighbor node.
    prior_state = chat._empty_incremental_state()
    prior_state = prior_state.__class__(
        window_cursor=1,
        global_window_count=1,
        running_node_max={"cond.neighbor": 0.4},
        node_source={"cond.neighbor": "nli"},
        running_edge_evidence={},
        seeded_keys=frozenset(),
        pair_count_total=1,
    )
    chat._INCREMENTAL_HOLDER.write(attempt_id, prior_state, MagicMock())

    patches = _base_patches(store, resolve_return=(("cond.bernoulli", "confirmed"),))

    result = await _run_handle_chat(db, session_id, patches)

    assert "apollo_reply" in result
    state = chat._INCREMENTAL_HOLDER.latest_state(attempt_id)
    assert state.running_node_max["cond.bernoulli"] == 1.0
    # Neighbor stays exactly where it was -- not lifted into the resolved/gray
    # boundary by the seed.
    assert state.running_node_max["cond.neighbor"] == 0.4
    assert "cond.neighbor" not in state.seeded_keys
