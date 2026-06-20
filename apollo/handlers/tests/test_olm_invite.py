"""P3.5 — Apollo OLM clarification-invite trigger logic.

Tests cover three layers in isolation:

    1. Pure helpers — find_low_conf_new_nodes, _node_summary, signal_to_metadata.
    2. The decide_invite gate — counter, cooldown, master flag, pattern.
    3. The apollo_llm.py suffix — invite text appears only when fired=True.

`decide_invite` is async + db-driven; we use SQLite-in-memory + the
ApolloSession / Message tables. Time is injected via `now=` so cooldown
tests are deterministic.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.conftest import TEST_SPACE_ID, TEST_USER_ID
from apollo.handlers.olm_invite import (
    COOLDOWN_SECONDS,
    LOW_CONF_THRESHOLD,
    OlmInviteSignal,
    decide_invite,
    find_low_conf_new_nodes,
    signal_to_metadata,
)
from apollo.ontology import build_node
from apollo.persistence.models import (
    ApolloSession,
    Message,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
)
from database.models import Base

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _eq(node_id: str, conf: float):
    return build_node(
        node_type="equation", node_id=node_id, attempt_id=1, source="parser",
        content={"symbolic": "x", "label": ""},
        parser_confidence=conf,
    )


def test_find_low_conf_new_nodes_filters_by_threshold():
    nodes = [_eq("a", 0.5), _eq("b", 0.9), _eq("c", 0.69)]
    out = find_low_conf_new_nodes(nodes)
    assert {n.node_id for n in out} == {"a", "c"}


def test_find_low_conf_new_nodes_threshold_is_0_7():
    """0.7 is the documented constant; this test pins it so a future
    change breaks the test on purpose."""
    assert LOW_CONF_THRESHOLD == 0.7
    out = find_low_conf_new_nodes([_eq("x", 0.7)])
    assert out == []  # 0.7 is NOT below 0.7


def test_signal_to_metadata_strips_summary():
    """Summary is redundant with the Neo4j node — only the gate fields
    persist on Message.metadata."""
    sig = OlmInviteSignal(
        fired=True, entry_id="eq1", summary="A1*v1 - A2*v2",
        low_conf_pattern_this_turn=True,
    )
    assert signal_to_metadata(sig) == {"fired": True, "entry_id": "eq1"}


# ---------------------------------------------------------------------------
# decide_invite — async, DB-backed
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        ApolloSession.__table__,
        ProblemAttempt.__table__,
        Message.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda sc: Base.metadata.create_all(sc, tables=tables))
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture
async def session(db: AsyncSession):
    s = ApolloSession(
        user_id=TEST_USER_ID, search_space_id=TEST_SPACE_ID, concept_id=1,
        status=SessionStatus.active.value, phase=SessionPhase.TEACHING.value,
    )
    db.add(s)
    await db.flush()
    a = ProblemAttempt(session_id=s.id, problem_id="p1", difficulty="intro")
    db.add(a)
    await db.commit()
    await db.refresh(s); await db.refresh(a)
    return s, a


async def _add_student_turn(db, session_id, attempt_id, idx, *, low_conf=False):
    db.add(Message(
        session_id=session_id, attempt_id=attempt_id, role="student",
        content="x", turn_index=idx,
        message_metadata={"low_conf_pattern": True} if low_conf else None,
    ))
    await db.commit()


async def _add_apollo_turn(
    db, session_id, attempt_id, idx, *, fired=False, when: datetime | None = None,
):
    msg = Message(
        session_id=session_id, attempt_id=attempt_id, role="apollo",
        content="x", turn_index=idx,
        message_metadata={"olm_invite": {"fired": fired, "entry_id": "y"}},
    )
    if when is not None:
        msg.created_at = when
    db.add(msg)
    await db.commit()


@pytest.mark.asyncio
async def test_decide_no_pattern_returns_unfired(monkeypatch, db, session):
    monkeypatch.setenv("APOLLO_OLM_INVITES_ENABLED", "1")
    s, _ = session
    sig = await decide_invite(
        db=db, session_id=s.id, new_low_conf_nodes=[],
    )
    assert sig.fired is False
    assert sig.low_conf_pattern_this_turn is False


@pytest.mark.asyncio
async def test_decide_master_flag_off_never_fires(monkeypatch, db, session):
    """When the master flag is off, the analytics flag still rides — but
    the actual invite never fires regardless of counter."""
    monkeypatch.delenv("APOLLO_OLM_INVITES_ENABLED", raising=False)
    s, a = session
    # Seed two prior low-conf student turns so counter would hit threshold.
    await _add_student_turn(db, s.id, a.id, 0, low_conf=True)
    await _add_student_turn(db, s.id, a.id, 1, low_conf=True)

    sig = await decide_invite(
        db=db, session_id=s.id,
        new_low_conf_nodes=[_eq("eq1", 0.5)],
    )
    assert sig.fired is False
    assert sig.low_conf_pattern_this_turn is True


@pytest.mark.asyncio
async def test_decide_first_pattern_below_counter_threshold(
    monkeypatch, db, session,
):
    """First low-conf turn ever — counter=1, below threshold → no fire."""
    monkeypatch.setenv("APOLLO_OLM_INVITES_ENABLED", "1")
    s, _ = session
    sig = await decide_invite(
        db=db, session_id=s.id,
        new_low_conf_nodes=[_eq("eq1", 0.4)],
    )
    assert sig.fired is False
    assert sig.low_conf_pattern_this_turn is True


@pytest.mark.asyncio
async def test_decide_fires_on_second_pattern_no_recent_invite(
    monkeypatch, db, session,
):
    """Second low-conf turn (counter=2) with no prior invite → fires."""
    monkeypatch.setenv("APOLLO_OLM_INVITES_ENABLED", "1")
    s, a = session
    await _add_student_turn(db, s.id, a.id, 0, low_conf=True)
    sig = await decide_invite(
        db=db, session_id=s.id,
        new_low_conf_nodes=[_eq("eq1", 0.4), _eq("eq2", 0.55)],
    )
    assert sig.fired is True
    # Lowest-confidence node wins.
    assert sig.entry_id == "eq1"
    assert sig.summary  # node summary populated


@pytest.mark.asyncio
async def test_decide_respects_cooldown(monkeypatch, db, session):
    """Counter past threshold AND a recent fired invite within cooldown → no fire."""
    monkeypatch.setenv("APOLLO_OLM_INVITES_ENABLED", "1")
    s, a = session
    # Two past low-conf turns → counter would be 3 this turn.
    await _add_student_turn(db, s.id, a.id, 0, low_conf=True)
    await _add_student_turn(db, s.id, a.id, 1, low_conf=True)
    # An apollo invite fired 30s ago — well within the 60s cooldown.
    now = datetime.now(UTC)
    await _add_apollo_turn(
        db, s.id, a.id, 2, fired=True,
        when=now - timedelta(seconds=30),
    )

    sig = await decide_invite(
        db=db, session_id=s.id,
        new_low_conf_nodes=[_eq("eq1", 0.4)],
        now=now,
    )
    assert sig.fired is False
    assert sig.low_conf_pattern_this_turn is True


@pytest.mark.asyncio
async def test_decide_fires_after_cooldown_elapsed(monkeypatch, db, session):
    monkeypatch.setenv("APOLLO_OLM_INVITES_ENABLED", "1")
    s, a = session
    await _add_student_turn(db, s.id, a.id, 0, low_conf=True)
    now = datetime.now(UTC)
    # Last invite was COOLDOWN_SECONDS+10s ago.
    await _add_apollo_turn(
        db, s.id, a.id, 1, fired=True,
        when=now - timedelta(seconds=COOLDOWN_SECONDS + 10),
    )

    sig = await decide_invite(
        db=db, session_id=s.id,
        new_low_conf_nodes=[_eq("eq1", 0.4)],
        now=now,
    )
    assert sig.fired is True


@pytest.mark.asyncio
async def test_decide_unfired_apollo_turns_dont_block_cooldown(
    monkeypatch, db, session,
):
    """An apollo turn with olm_invite.fired=False shouldn't count as
    'last invite' — cooldown only triggers off real fires."""
    monkeypatch.setenv("APOLLO_OLM_INVITES_ENABLED", "1")
    s, a = session
    await _add_student_turn(db, s.id, a.id, 0, low_conf=True)
    # An apollo turn that DID NOT fire an invite, recent.
    now = datetime.now(UTC)
    await _add_apollo_turn(
        db, s.id, a.id, 1, fired=False, when=now - timedelta(seconds=5),
    )

    sig = await decide_invite(
        db=db, session_id=s.id,
        new_low_conf_nodes=[_eq("eq1", 0.4)],
        now=now,
    )
    assert sig.fired is True


@pytest.mark.asyncio
async def test_decide_picks_lowest_confidence_node_as_entry(
    monkeypatch, db, session,
):
    monkeypatch.setenv("APOLLO_OLM_INVITES_ENABLED", "1")
    s, a = session
    await _add_student_turn(db, s.id, a.id, 0, low_conf=True)
    sig = await decide_invite(
        db=db, session_id=s.id,
        new_low_conf_nodes=[_eq("a", 0.65), _eq("b", 0.30), _eq("c", 0.55)],
    )
    assert sig.fired is True
    assert sig.entry_id == "b"


# NOTE: the apollo_llm OLM-invite suffix tests were removed in diff-at-Done
# v1 — draft_reply no longer accepts an olm_invite arg. The olm_invite module
# logic below (decide_invite / find_low_conf_new_nodes / signal_to_metadata)
# is retained: the module stays defined though it is unwired from chat.py.
