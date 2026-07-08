"""Tests for T11: chat.py incremental V2 wiring (integration spec §2.2/§3.1/
§5.1/§5.5/§8.3).

Acceptance criteria under test:

* Gate OFF (incl. RESOLVER_V2=ON, RANKER=OFF -- the H1 matrix row) -> no
  background thread starts, no session state mutated, no NLI budget spent.
* The reply path never awaits the current turn's incremental job.
* A second concurrent turn does not start a 2nd job (one-in-flight/attempt).
* A background exception leaves the snapshot at its prior/none value and
  never reaches the reply (H3).
* Cold worker / turn 1 -> no completed snapshot -> v1 (snapshot=None passed
  to run_clarification_detection).
* The cursor advances across turns on a warm worker.
"""

from __future__ import annotations

import asyncio
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
from apollo.resolver_v2.incremental_types import IncrementalSnapshot, IncrementalState
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
    """Every test gets a fresh process-local holder + task set (module
    globals persist across tests otherwise)."""
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


def _base_patches(store, *, mock_draft_reply=None, mock_detect=None):
    dr = mock_draft_reply if mock_draft_reply is not None else MagicMock(return_value="ok")
    detect = mock_detect if mock_detect is not None else AsyncMock(return_value=[])
    return [
        patch("apollo.handlers.chat.KGStore", return_value=store),
        patch("apollo.handlers.chat.parse_utterance", return_value=([], [])),
        patch("apollo.handlers.chat.draft_reply", new=dr),
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
        patch("apollo.handlers.chat.run_clarification_detection", new=detect),
        patch("apollo.handlers.chat.resolve_pending_clarifications", new=AsyncMock()),
        patch("apollo.handlers.chat.guard_clarification_reply", new=MagicMock(return_value="ok")),
    ]


async def _run_handle_chat(db, session_id, patches):
    from apollo.handlers.chat import handle_chat

    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[
        6
    ], patches[7], patches[8], patches[9], patches[10], patches[11]:
        return await handle_chat(db=db, neo=MagicMock(), session_id=session_id, message="hi")


# ---------------------------------------------------------------------------
# _v2_ranker_active gating (H1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "clar,v2,ranker,expected",
    [
        ("0", "0", "0", False),
        ("1", "0", "0", False),
        ("1", "1", "0", False),  # the specific H1 matrix row
        ("0", "1", "1", False),
        ("1", "0", "1", False),
        ("1", "1", "1", True),
    ],
)
def test_v2_ranker_active_matrix(monkeypatch, clar, v2, ranker, expected):
    monkeypatch.setenv(_FLAG_CLAR, clar)
    monkeypatch.setenv(_FLAG_V2, v2)
    monkeypatch.setenv(_FLAG_RANKER, ranker)
    assert chat._v2_ranker_active() is expected


# ---------------------------------------------------------------------------
# H1: gate OFF (incl. RESOLVER_V2=ON / RANKER=OFF) -> no thread, no mutation,
# no budget spend
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "clar,v2,ranker",
    [
        ("0", "0", "0"),
        ("1", "0", "0"),
        ("1", "1", "0"),  # RESOLVER_V2 ON, RANKER OFF -- the H1 row
        ("1", "0", "1"),
    ],
)
async def test_gate_off_no_background_kick(db_session_attempt, monkeypatch, clar, v2, ranker):
    monkeypatch.setenv(_FLAG_CLAR, clar)
    monkeypatch.setenv(_FLAG_V2, v2)
    monkeypatch.setenv(_FLAG_RANKER, ranker)
    db, session_id, attempt_id = db_session_attempt
    store = _fake_store()
    mock_detect = AsyncMock(return_value=[])
    patches = _base_patches(store, mock_detect=mock_detect)

    with patch("apollo.handlers.chat._maybe_kick_incremental_v2") as mock_kick:
        result = await _run_handle_chat(db, session_id, patches)

    assert "apollo_reply" in result
    mock_kick.assert_not_called()
    assert chat._INCREMENTAL_HOLDER._entries == {}
    assert chat._INCREMENTAL_BACKGROUND_TASKS == set()
    if clar == "1":
        # run_clarification_detection still runs the (v1) path, but with no snapshot.
        assert mock_detect.called
        assert mock_detect.call_args.kwargs["snapshot"] is None


@pytest.mark.asyncio
async def test_gate_on_kicks_and_passes_snapshot(db_session_attempt, monkeypatch):
    """Gate fully ON: _maybe_kick_incremental_v2 is invoked once and its
    return value flows into run_clarification_detection's snapshot kwarg."""
    monkeypatch.setenv(_FLAG_CLAR, "1")
    monkeypatch.setenv(_FLAG_V2, "1")
    monkeypatch.setenv(_FLAG_RANKER, "1")
    db, session_id, attempt_id = db_session_attempt
    store = _fake_store()
    mock_detect = AsyncMock(return_value=[])
    patches = _base_patches(store, mock_detect=mock_detect)

    fake_snapshot = MagicMock(spec=IncrementalSnapshot)
    mock_kick = AsyncMock(return_value=fake_snapshot)

    with patch("apollo.handlers.chat._maybe_kick_incremental_v2", new=mock_kick):
        result = await _run_handle_chat(db, session_id, patches)

    assert "apollo_reply" in result
    mock_kick.assert_awaited_once()
    assert mock_kick.call_args.kwargs["attempt_id"] == attempt_id
    assert mock_kick.call_args.kwargs["message"] == "hi"
    assert mock_detect.call_args.kwargs["snapshot"] is fake_snapshot


# ---------------------------------------------------------------------------
# _IncrementalHolder: single-writer + monotone guard
# ---------------------------------------------------------------------------


def _state(cursor: int) -> IncrementalState:
    return IncrementalState(
        window_cursor=cursor,
        global_window_count=cursor,
        running_node_max={},
        node_source={},
        running_edge_evidence={},
        seeded_keys=frozenset(),
        pair_count_total=0,
    )


def _snapshot(pair_count: int = 0) -> IncrementalSnapshot:
    return IncrementalSnapshot(
        node_credits={},
        edge_scores=(),
        node_cov=0.0,
        edge_cov=0.0,
        winning_path_index=0,
        gray=frozenset(),
        pair_count_this_turn=pair_count,
    )


def test_holder_try_acquire_one_in_flight():
    holder = chat._IncrementalHolder()
    assert holder.try_acquire(1) is True
    # Second attempt for the SAME attempt while still in flight -> refused.
    assert holder.try_acquire(1) is False
    # A DIFFERENT attempt is independent.
    assert holder.try_acquire(2) is True
    holder.release(1)
    assert holder.try_acquire(1) is True


def test_holder_write_monotone_guard_discards_regression():
    holder = chat._IncrementalHolder()
    holder.write(1, _state(5), _snapshot(1))
    assert holder.latest_state(1).window_cursor == 5
    # A write with a cursor NOT strictly greater is discarded.
    holder.write(1, _state(5), _snapshot(2))
    assert holder.latest_state(1).window_cursor == 5
    holder.write(1, _state(3), _snapshot(3))
    assert holder.latest_state(1).window_cursor == 5
    # A strictly greater cursor advances the state.
    holder.write(1, _state(9), _snapshot(4))
    assert holder.latest_state(1).window_cursor == 9


def test_holder_cold_attempt_has_no_snapshot():
    holder = chat._IncrementalHolder()
    assert holder.latest_snapshot(999) is None
    assert holder.latest_state(999) is None


# ---------------------------------------------------------------------------
# _run_incremental_v2_job: success writes state+snapshot, exception leaves
# the holder at prior/none (H3), and releases in_flight either way.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_job_success_writes_state_and_snapshot():
    attempt_id = 42
    chat._INCREMENTAL_HOLDER.try_acquire(attempt_id)
    new_state = _state(1)
    new_snapshot = _snapshot(3)

    with patch(
        "apollo.handlers.chat.score_turn", return_value=(new_state, new_snapshot)
    ) as mock_score:
        await chat._run_incremental_v2_job(
            attempt_id=attempt_id,
            state=chat._empty_incremental_state(),
            all_student_turns=("hello",),
            reference_graph=MagicMock(),
            problem_payload={},
            v1_resolved_keys=frozenset(),
            ref_nodes=(),
            params=MagicMock(),
            nli=None,
            grayzone_fn=None,
        )

    assert mock_score.called
    assert chat._INCREMENTAL_HOLDER.latest_state(attempt_id) is new_state
    assert chat._INCREMENTAL_HOLDER.latest_snapshot(attempt_id) is new_snapshot
    # in-flight is released after completion -> a later turn may kick again.
    assert chat._INCREMENTAL_HOLDER.try_acquire(attempt_id) is True


@pytest.mark.asyncio
async def test_job_exception_leaves_snapshot_at_prior_value():
    """H3: an exception inside the background job never escapes, and the
    holder's snapshot stays at whatever it was before (prior turn's, or
    none if there was never a completed one)."""
    attempt_id = 7
    chat._INCREMENTAL_HOLDER.try_acquire(attempt_id)

    with patch("apollo.handlers.chat.score_turn", side_effect=RuntimeError("boom")):
        # Must not raise.
        await chat._run_incremental_v2_job(
            attempt_id=attempt_id,
            state=chat._empty_incremental_state(),
            all_student_turns=("hello",),
            reference_graph=MagicMock(),
            problem_payload={},
            v1_resolved_keys=frozenset(),
            ref_nodes=(),
            params=MagicMock(),
            nli=None,
            grayzone_fn=None,
        )

    assert chat._INCREMENTAL_HOLDER.latest_snapshot(attempt_id) is None
    assert chat._INCREMENTAL_HOLDER.latest_state(attempt_id) is None
    # in-flight was still released despite the exception.
    assert chat._INCREMENTAL_HOLDER.try_acquire(attempt_id) is True


@pytest.mark.asyncio
async def test_job_exception_preserves_prior_completed_snapshot():
    attempt_id = 8
    prior_state, prior_snapshot = _state(2), _snapshot(1)
    chat._INCREMENTAL_HOLDER.write(attempt_id, prior_state, prior_snapshot)
    chat._INCREMENTAL_HOLDER.try_acquire(attempt_id)

    with patch("apollo.handlers.chat.score_turn", side_effect=RuntimeError("boom")):
        await chat._run_incremental_v2_job(
            attempt_id=attempt_id,
            state=prior_state,
            all_student_turns=("hello", "world"),
            reference_graph=MagicMock(),
            problem_payload={},
            v1_resolved_keys=frozenset(),
            ref_nodes=(),
            params=MagicMock(),
            nli=None,
            grayzone_fn=None,
        )

    assert chat._INCREMENTAL_HOLDER.latest_snapshot(attempt_id) is prior_snapshot
    assert chat._INCREMENTAL_HOLDER.latest_state(attempt_id) is prior_state


# ---------------------------------------------------------------------------
# _maybe_kick_incremental_v2: cold turn 1, one-in-flight, never-await, cursor
# advance across turns.
# ---------------------------------------------------------------------------


def _kick_collaborator_patches(*, ref_nodes=(), reference_graph=None):
    return [
        patch("apollo.handlers.chat.load_student_turns", new=AsyncMock(return_value=())),
        patch(
            "apollo.handlers.chat._v1_resolved_keys_for_turn", return_value=frozenset()
        ),
        patch(
            "apollo.handlers.chat.build_reference_canonical",
            return_value=reference_graph or MagicMock(),
        ),
        patch("apollo.handlers.chat.load_views", return_value={}),
        patch("apollo.handlers.chat.build_ref_nodes", return_value=ref_nodes),
        patch("apollo.handlers.chat.load_params", return_value=MagicMock()),
        patch("apollo.handlers.chat.get_adjudicator", return_value=None),
    ]


@pytest.mark.asyncio
async def test_cold_turn_returns_none_and_kicks_a_job(db_session_attempt):
    """Turn 1 / cold worker: no completed snapshot exists yet -> returns None
    (the caller falls back to v1), and a background job IS scheduled."""
    db, _session_id, attempt_id = db_session_attempt
    patches = _kick_collaborator_patches()
    never_finishes = asyncio.Event()

    async def _blocking_job(**kwargs):
        await never_finishes.wait()

    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patches[6],
        patch("apollo.handlers.chat._run_incremental_v2_job", new=_blocking_job),
    ):
        result = await chat._maybe_kick_incremental_v2(
            db,
            attempt_id=attempt_id,
            message="hello",
            student_graph=KGGraph(),
            candidates=(),
            symbolic_mappings={},
            problem_payload={},
        )

    assert result is None
    assert len(chat._INCREMENTAL_BACKGROUND_TASKS) == 1
    # Never-awaited: the call above returned even though the background job
    # is still blocked on `never_finishes`.
    never_finishes.set()
    # Let the background task drain before the test ends.
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_second_concurrent_turn_does_not_start_a_second_job(db_session_attempt):
    db, _session_id, attempt_id = db_session_attempt
    patches = _kick_collaborator_patches()
    never_finishes = asyncio.Event()
    job_started = asyncio.Event()

    async def _blocking_job(**kwargs):
        job_started.set()
        await never_finishes.wait()

    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patches[6],
        patch("apollo.handlers.chat._run_incremental_v2_job", new=_blocking_job),
    ):
        first = await chat._maybe_kick_incremental_v2(
            db,
            attempt_id=attempt_id,
            message="turn one",
            student_graph=KGGraph(),
            candidates=(),
            symbolic_mappings={},
            problem_payload={},
        )
        await job_started.wait()
        assert len(chat._INCREMENTAL_BACKGROUND_TASKS) == 1

        second = await chat._maybe_kick_incremental_v2(
            db,
            attempt_id=attempt_id,
            message="turn two",
            student_graph=KGGraph(),
            candidates=(),
            symbolic_mappings={},
            problem_payload={},
        )

    assert first is None
    assert second is None  # still no completed snapshot -- the first job never finished
    # No second task was scheduled while the first is still in flight.
    assert len(chat._INCREMENTAL_BACKGROUND_TASKS) == 1

    never_finishes.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_cursor_advances_across_turns_on_warm_worker(db_session_attempt):
    """After a completed job, the NEXT kick reads the stored state (cursor
    advanced) and returns the completed snapshot as `prior_snapshot`."""
    db, _session_id, attempt_id = db_session_attempt
    patches = _kick_collaborator_patches()

    seen_states: list[IncrementalState] = []

    def _fake_score_turn(state, **kwargs):
        seen_states.append(state)
        new_cursor = state.window_cursor + 1
        new_state = IncrementalState(
            window_cursor=new_cursor,
            global_window_count=new_cursor,
            running_node_max={},
            node_source={},
            running_edge_evidence={},
            seeded_keys=frozenset(),
            pair_count_total=0,
        )
        return new_state, _snapshot(new_cursor)

    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patches[6],
        patch("apollo.handlers.chat.score_turn", side_effect=_fake_score_turn),
    ):
        first_prior = await chat._maybe_kick_incremental_v2(
            db,
            attempt_id=attempt_id,
            message="turn one",
            student_graph=KGGraph(),
            candidates=(),
            symbolic_mappings={},
            problem_payload={},
        )
        # Let the background task (fire-and-forget) actually run to completion
        # before kicking the next turn.
        await asyncio.gather(*chat._INCREMENTAL_BACKGROUND_TASKS)

        second_prior = await chat._maybe_kick_incremental_v2(
            db,
            attempt_id=attempt_id,
            message="turn two",
            student_graph=KGGraph(),
            candidates=(),
            symbolic_mappings={},
            problem_payload={},
        )
        await asyncio.gather(*chat._INCREMENTAL_BACKGROUND_TASKS)

    assert first_prior is None  # cold: nothing completed yet when turn 1 was kicked
    assert second_prior is not None
    assert second_prior.pair_count_this_turn == 1  # completed by the turn-1 job
    # The turn-2 job was handed the cursor-advanced state from turn 1.
    assert seen_states[1].window_cursor == 1


# ---------------------------------------------------------------------------
# _v1_resolved_keys_for_turn
# ---------------------------------------------------------------------------


def test_v1_resolved_keys_for_turn_excludes_unresolved_and_misconceptions():
    ResolvedNode = MagicMock
    resolved = (
        MagicMock(resolution="resolved", resolved_key="cond.bernoulli"),
        MagicMock(resolution="resolved", resolved_key="misc.density_ignored"),
        MagicMock(resolution="unresolved", resolved_key=None),
    )
    fake_result = MagicMock(resolved=resolved)

    with patch("apollo.handlers.chat.resolve_attempt", return_value=fake_result):
        keys = chat._v1_resolved_keys_for_turn(KGGraph(), (), {})

    assert keys == frozenset({"cond.bernoulli"})
