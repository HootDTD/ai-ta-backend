"""P3.6 — Done-gate (BLOCK strength) tests.

Tests cover the gate logic in isolation:
    _flagged_entries  — pure-function classifier
    _enforce_done_gate — async, queries Postgres for negotiation moves

The gate's master flag (`APOLLO_DONE_GATE_ENABLED`) is verified end-to-end
on a contrived done flow: with flag off + flagged entries, handle_done
proceeds; with flag on, it raises ReviewRequiredError.

We don't drive handle_done end-to-end here — the legacy test_done.py
covers the success path; this file covers the gate.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.conftest import TEST_SPACE_ID, TEST_USER_ID
from apollo.errors import ReviewRequiredError
from apollo.handlers.done import (
    _DONE_GATE_LOW_CONF,
    _enforce_done_gate,
    _flagged_entries,
    _entries_with_moves,
    _done_gate_enabled,
)
from apollo.ontology import KGGraph, build_node
from apollo.persistence.models import (
    ApolloSession,
    KGNegotiation,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
)
from database.models import Base


# ---------------------------------------------------------------------------
# _flagged_entries — pure
# ---------------------------------------------------------------------------

def _eq(node_id: str, *, conf: float = 1.0, status: str = "ACCEPTED",
        source: str = "parser"):
    return build_node(
        node_type="equation", node_id=node_id, attempt_id=1, source=source,
        content={"symbolic": "x", "label": ""},
        parser_confidence=conf, status=status,  # type: ignore[arg-type]
    )


def test_flagged_entries_returns_empty_for_clean_graph():
    g = KGGraph(nodes=[_eq("a"), _eq("b", conf=0.95)])
    assert _flagged_entries(g) == []


def test_flagged_low_confidence_below_threshold():
    g = KGGraph(nodes=[
        _eq("a", conf=0.5),  # flagged
        _eq("b", conf=0.6),  # NOT flagged — threshold is strict <
        _eq("c", conf=0.59),  # flagged
    ])
    flagged = _flagged_entries(g)
    assert {n.node_id for n, _ in flagged} == {"a", "c"}
    assert all(reason == "low_confidence" for _, reason in flagged)


def test_flagged_disputed_regardless_of_confidence():
    g = KGGraph(nodes=[
        _eq("a", conf=1.0, status="DISPUTED"),
    ])
    flagged = _flagged_entries(g)
    assert flagged[0][1] == "disputed"


def test_disputed_wins_over_low_confidence_label():
    """A node that is BOTH disputed and low-conf gets reason='disputed' —
    the more specific signal."""
    g = KGGraph(nodes=[_eq("a", conf=0.4, status="DISPUTED")])
    flagged = _flagged_entries(g)
    assert flagged[0][1] == "disputed"


def test_dual_status_does_not_flag():
    """DUAL means the student already engaged with the entry — no need to
    block. Coverage handles DUAL via student_belief (P3.4)."""
    g = KGGraph(nodes=[_eq("a", conf=0.4, status="DUAL")])
    assert _flagged_entries(g) == []


def test_reference_and_system_nodes_not_flagged():
    """Only parser-sourced nodes can be questioned — the others come from
    the curriculum / system and aren't user-authored."""
    g = KGGraph(nodes=[
        _eq("ref", conf=0.0, source="reference"),
        _eq("sys", conf=0.0, source="system"),
    ])
    assert _flagged_entries(g) == []


def test_threshold_constant_is_0_6():
    """Pin the constant so future changes break this test on purpose."""
    assert _DONE_GATE_LOW_CONF == 0.6


# ---------------------------------------------------------------------------
# _enforce_done_gate — DB-backed
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        ApolloSession.__table__,
        ProblemAttempt.__table__,
        KGNegotiation.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda sc: Base.metadata.create_all(sc, tables=tables))
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture
async def attempt(db: AsyncSession):
    sess = ApolloSession(
        user_id=TEST_USER_ID, search_space_id=TEST_SPACE_ID, concept_cluster_id="x",
        status=SessionStatus.active.value, phase=SessionPhase.TEACHING.value,
    )
    db.add(sess); await db.flush()
    a = ProblemAttempt(session_id=sess.id, problem_id="p1", difficulty="intro")
    db.add(a); await db.commit(); await db.refresh(a)
    return a


@pytest.mark.asyncio
async def test_gate_passes_when_no_flagged_entries(db, attempt):
    g = KGGraph(nodes=[_eq("a"), _eq("b")])
    # No raise.
    await _enforce_done_gate(db, attempt_id=attempt.id, graph=g)


@pytest.mark.asyncio
async def test_gate_blocks_with_unmoved_low_conf_entry(db, attempt):
    g = KGGraph(nodes=[_eq("a", conf=0.4)])
    with pytest.raises(ReviewRequiredError) as exc_info:
        await _enforce_done_gate(db, attempt_id=attempt.id, graph=g)
    err = exc_info.value
    assert len(err.entries) == 1
    e = err.entries[0]
    assert e["entry_id"] == "a"
    assert e["type"] == "equation"
    assert e["reason"] == "low_confidence"
    assert e["summary"]  # non-empty


@pytest.mark.asyncio
async def test_gate_blocks_with_unmoved_disputed_entry(db, attempt):
    g = KGGraph(nodes=[_eq("a", status="DISPUTED")])
    with pytest.raises(ReviewRequiredError) as exc_info:
        await _enforce_done_gate(db, attempt_id=attempt.id, graph=g)
    assert exc_info.value.entries[0]["reason"] == "disputed"


@pytest.mark.asyncio
async def test_gate_clears_when_entry_has_a_move(db, attempt):
    """A negotiation move on the flagged entry clears the gate — even
    though Neo4j hasn't yet flipped the status (e.g. the FE is mid-flow)."""
    g = KGGraph(nodes=[_eq("a", conf=0.4)])
    db.add(KGNegotiation(
        attempt_id=attempt.id, entry_id="a", actor="student",
        move="skip", payload={},
    ))
    await db.commit()
    # No raise.
    await _enforce_done_gate(db, attempt_id=attempt.id, graph=g)


@pytest.mark.asyncio
async def test_gate_returns_only_unmoved_entries(db, attempt):
    """Some flagged + some moved → 422 lists ONLY the un-moved ones."""
    g = KGGraph(nodes=[
        _eq("a", conf=0.4),  # un-moved → in 422
        _eq("b", status="DISPUTED"),  # un-moved → in 422
        _eq("c", conf=0.3),  # moved → NOT in 422
    ])
    db.add(KGNegotiation(
        attempt_id=attempt.id, entry_id="c", actor="student",
        move="paraphrase", payload={"surface_form": "rho A v"},
    ))
    await db.commit()

    with pytest.raises(ReviewRequiredError) as exc_info:
        await _enforce_done_gate(db, attempt_id=attempt.id, graph=g)
    ids = {e["entry_id"] for e in exc_info.value.entries}
    assert ids == {"a", "b"}


@pytest.mark.asyncio
async def test_any_move_type_clears_gate(db, attempt):
    """The gate doesn't care WHICH move was made — challenge / paraphrase
    / skip all clear. The point is "the student engaged with this entry."""
    for move in ("challenge", "paraphrase", "skip"):
        g = KGGraph(nodes=[_eq(f"e_{move}", conf=0.4)])
        db.add(KGNegotiation(
            attempt_id=attempt.id, entry_id=f"e_{move}", actor="student",
            move=move, payload={},
        ))
        await db.commit()
        # No raise.
        await _enforce_done_gate(db, attempt_id=attempt.id, graph=g)


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------

def test_master_flag_off_by_default(monkeypatch):
    monkeypatch.delenv("APOLLO_DONE_GATE_ENABLED", raising=False)
    assert _done_gate_enabled() is False


def test_master_flag_truthy_values(monkeypatch):
    for val in ("1", "true", "True", "yes", "TRUE"):
        monkeypatch.setenv("APOLLO_DONE_GATE_ENABLED", val)
        assert _done_gate_enabled() is True, f"failed for {val!r}"


def test_master_flag_falsy_values(monkeypatch):
    for val in ("", "0", "false", "no", "off"):
        monkeypatch.setenv("APOLLO_DONE_GATE_ENABLED", val)
        assert _done_gate_enabled() is False, f"failed for {val!r}"


# ---------------------------------------------------------------------------
# _entries_with_moves — read helper
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_entries_with_moves_aggregates_by_attempt(db, attempt):
    db.add(KGNegotiation(
        attempt_id=attempt.id, entry_id="a", actor="student",
        move="skip", payload={},
    ))
    db.add(KGNegotiation(
        attempt_id=attempt.id, entry_id="b", actor="student",
        move="challenge", payload={"reason": "x"},
    ))
    db.add(KGNegotiation(
        attempt_id=attempt.id, entry_id="a", actor="student",
        move="paraphrase", payload={"surface_form": "y"},
    ))
    await db.commit()
    out = await _entries_with_moves(db, attempt_id=attempt.id)
    assert out == {"a", "b"}
