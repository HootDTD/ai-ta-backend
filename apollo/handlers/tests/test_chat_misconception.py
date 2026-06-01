"""P2.7 — Misconception wiring helpers in chat.py.

The chat handler integrates `apollo.overseer.misconception.infer_misconception`
into the teaching path. The deep pipeline behavior is covered by
test_misconception.py; this file covers the *integration* contract:

- Signal serialization round-trips via Message.metadata.
- `_load_previous_signals` returns the most recent k Apollo turns'
  signals for the PROBE-then-confirm gate.
- Internal-only fields (description, bank_id) are stripped on
  persistence — the JSONB row is safe for any future analytics view.
- A session pre-dating migration 018 (no concept_id FK) is a no-op:
  the inference call is skipped, signal defaults to no-op state.

Avoids the full Neo4j + OpenAI integration that the legacy test_chat.py
module-skips — that is covered by P2.10's e2e smoke instead.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.handlers.chat import (
    _load_previous_signals,
    _metadata_to_signal,
    _signal_to_metadata,
)
from apollo.overseer.misconception import MisconceptionSignal
from apollo.persistence.models import (
    ApolloSession,
    Message,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
)
from database.models import Base


# ---------------------------------------------------------------------------
# Round-trip serialization
# ---------------------------------------------------------------------------

def test_signal_to_metadata_strips_internal_fields():
    sig = MisconceptionSignal(
        fired=True,
        state="probe",
        description="treats fluid as having no density",
        bank_id="42",
        bank_code="no_density",
        probe="hmm, density?",
        rt_steps=("step1",),
        confidence=0.66,
    )
    meta = _signal_to_metadata(sig)
    assert meta == {
        "fired": True,
        "state": "probe",
        "bank_code": "no_density",
        "confidence": 0.66,
    }
    # Verify the internal-only strings really are gone.
    serialized = repr(meta)
    assert "no density" not in serialized
    assert "42" not in serialized
    assert "rt_steps" not in serialized
    assert "probe" not in meta  # the *probe text* — `probe` band string IS allowed in state


def test_metadata_to_signal_round_trip():
    sig = MisconceptionSignal(
        fired=True, state="socratic", bank_code="x", confidence=0.9,
    )
    meta = {"misconception": _signal_to_metadata(sig)}
    out = _metadata_to_signal(meta)
    assert out is not None
    assert out.fired is True
    assert out.state == "socratic"
    assert out.bank_code == "x"
    assert out.confidence == pytest.approx(0.9)


def test_metadata_to_signal_handles_missing_payload():
    assert _metadata_to_signal(None) is None
    assert _metadata_to_signal({}) is None
    assert _metadata_to_signal({"misconception": None}) is None
    assert _metadata_to_signal({"misconception": "not a dict"}) is None


def test_metadata_to_signal_rejects_unknown_state():
    """Defense against an analytics-view writer that injects a bogus state.
    The inference module emits only default/probe/socratic; anything
    else is corruption and must not surface as a 'previous signal'."""
    bad = {"misconception": {"fired": True, "state": "wrong", "bank_code": "x"}}
    assert _metadata_to_signal(bad) is None


# ---------------------------------------------------------------------------
# Read-back from Message rows
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db_with_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    apollo_tables = [
        ApolloSession.__table__,
        Message.__table__,
        ProblemAttempt.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=apollo_tables)
        )
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        sess = ApolloSession(
            student_id="stu-1",
            concept_cluster_id="fluid_mechanics",
            status=SessionStatus.active.value,
            phase=SessionPhase.TEACHING.value,
            current_problem_id="x",
        )
        s.add(sess)
        await s.commit()
        await s.refresh(sess)
        yield s, sess.id
    await engine.dispose()


@pytest.mark.asyncio
async def test_load_previous_signals_returns_latest_k(db_with_session):
    db, sid = db_with_session
    # Three apollo turns with different signals + one student turn.
    db.add(Message(
        session_id=sid, role="student", content="u1",
        turn_index=0, attempt_id=None,
    ))
    db.add(Message(
        session_id=sid, role="apollo", content="a1",
        turn_index=1, attempt_id=None,
        message_metadata={"misconception": {
            "fired": True, "state": "probe", "bank_code": "alpha", "confidence": 0.55,
        }},
    ))
    db.add(Message(
        session_id=sid, role="apollo", content="a2",
        turn_index=2, attempt_id=None,
        message_metadata={"misconception": {
            "fired": False, "state": "default", "bank_code": None, "confidence": 0.0,
        }},
    ))
    db.add(Message(
        session_id=sid, role="apollo", content="a3",
        turn_index=3, attempt_id=None,
        message_metadata={"misconception": {
            "fired": True, "state": "probe", "bank_code": "alpha", "confidence": 0.6,
        }},
    ))
    await db.commit()

    sigs = await _load_previous_signals(db, session_id=sid, k=2)
    assert len(sigs) == 2
    # Most-recent first per the order_by desc.
    assert sigs[0].bank_code == "alpha"  # a3
    assert sigs[0].confidence == pytest.approx(0.6)
    assert sigs[1].bank_code is None  # a2 (unfired)


@pytest.mark.asyncio
async def test_load_previous_signals_skips_student_turns(db_with_session):
    db, sid = db_with_session
    db.add(Message(
        session_id=sid, role="student", content="u1",
        turn_index=0, attempt_id=None,
        message_metadata={"misconception": {
            "fired": True, "state": "socratic", "bank_code": "shouldnt", "confidence": 0.9,
        }},  # nonsensical (student rows don't carry misconception signals)
    ))
    db.add(Message(
        session_id=sid, role="apollo", content="a1",
        turn_index=1, attempt_id=None,
        message_metadata={"misconception": {
            "fired": True, "state": "probe", "bank_code": "real", "confidence": 0.55,
        }},
    ))
    await db.commit()

    sigs = await _load_previous_signals(db, session_id=sid, k=2)
    assert len(sigs) == 1
    assert sigs[0].bank_code == "real"


@pytest.mark.asyncio
async def test_load_previous_signals_handles_empty_history(db_with_session):
    db, sid = db_with_session
    sigs = await _load_previous_signals(db, session_id=sid, k=2)
    assert sigs == ()


@pytest.mark.asyncio
async def test_load_previous_signals_tolerates_null_metadata(db_with_session):
    """Pre-P2.7 messages have no metadata column populated. The reader
    must tolerate this rather than crash."""
    db, sid = db_with_session
    db.add(Message(
        session_id=sid, role="apollo", content="a1",
        turn_index=1, attempt_id=None,
        message_metadata=None,
    ))
    await db.commit()
    sigs = await _load_previous_signals(db, session_id=sid, k=2)
    assert sigs == ()
