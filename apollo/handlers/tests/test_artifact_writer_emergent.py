"""Emergent-store write-path wiring in write_artifacts (memo increment 1).

Proves flag-OFF dormancy (the canonical artifact is byte-identical and ZERO
observation rows are written) and flag-ON wiring (the store is invoked once,
with the CANONICAL payload and the session's identity — never the pair role).
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.handlers import artifact_writer
from apollo.persistence.models import (
    ApolloSession,
    Clarification,
    GradingArtifact,
    Misconception,
    MisconceptionObservation,
    ProblemAttempt,
    SessionPhase,
    SessionStatus,
)
from database.models import Base

_USER = "a0000000-0000-4000-8000-000000000001"


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sc: Base.metadata.create_all(
                sc,
                tables=[
                    ApolloSession.__table__,
                    ProblemAttempt.__table__,
                    Clarification.__table__,
                    GradingArtifact.__table__,
                    MisconceptionObservation.__table__,
                    Misconception.__table__,
                ],
            )
        )
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        yield s
    await engine.dispose()


async def _mk(db):
    sess = ApolloSession(
        user_id=_USER,
        search_space_id=1,
        concept_id=42,
        status=SessionStatus.active.value,
        phase=SessionPhase.TEACHING.value,
        current_problem_id="p1",
    )
    db.add(sess)
    await db.flush()
    attempt = ProblemAttempt(
        session_id=sess.id, problem_id="p1", difficulty="intro", result="graded"
    )
    db.add(attempt)
    await db.flush()
    return sess, attempt


_COVERAGE = {"per_step": {"n1": "covered", "n2": "missing"}, "confidences": {"n1": 0.9}}
_RUBRIC = {"overall": {"score": 80}}


async def _run(db, sess, attempt):
    return await artifact_writer.write_artifacts(
        db,
        attempt=attempt,
        sess=sess,
        shadow=None,
        coverage=_COVERAGE,
        rubric=_RUBRIC,
        served="llm_fallback",
        graph_failure=None,
        latency_ms=123,
    )


async def _obs_count(db) -> int:
    return int(
        (await db.execute(select(func.count()).select_from(MisconceptionObservation))).scalar_one()
    )


@pytest.mark.asyncio
async def test_flag_off_is_dormant_zero_observation_rows(monkeypatch):
    monkeypatch.delenv("APOLLO_EMERGENT_MISCONCEPTIONS", raising=False)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sc: Base.metadata.create_all(
                sc,
                tables=[
                    ApolloSession.__table__,
                    ProblemAttempt.__table__,
                    Clarification.__table__,
                    GradingArtifact.__table__,
                    MisconceptionObservation.__table__,
                    Misconception.__table__,
                ],
            )
        )
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as db:
        sess, attempt = await _mk(db)
        payload = await _run(db, sess, attempt)
        assert payload is not None
        # Canonical artifact written, but the store stayed dormant.
        assert (
            await db.execute(select(func.count()).select_from(GradingArtifact))
        ).scalar_one() == 1
        assert await _obs_count(db) == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_flag_on_payload_byte_identical_and_llm_path_writes_no_rows(db, monkeypatch):
    """Flag ON: the returned canonical payload is byte-identical to the flag-OFF
    run (the store never mutates the grade), and an LLM-path artifact (no
    detected misconceptions) writes zero observation rows — cold-store is a no-op."""
    monkeypatch.delenv("APOLLO_EMERGENT_MISCONCEPTIONS", raising=False)
    sess, attempt = await _mk(db)
    off_payload = await _run(db, sess, attempt)

    # Fresh state, flag ON, same inputs.
    await db.execute(GradingArtifact.__table__.delete())
    await db.commit()
    monkeypatch.setenv("APOLLO_EMERGENT_MISCONCEPTIONS", "1")
    on_payload = await _run(db, sess, attempt)

    assert json.dumps(on_payload, sort_keys=True) == json.dumps(off_payload, sort_keys=True)
    assert await _obs_count(db) == 0  # LLM artifact carries no misconceptions


@pytest.mark.asyncio
async def test_flag_on_invokes_store_with_canonical_identity(db, monkeypatch):
    """Flag ON: record_observations_from_canonical is called exactly once, with
    the CANONICAL payload (grader_used=llm_fallback here) and the session's
    identity fields — never a pair-role payload."""
    monkeypatch.setenv("APOLLO_EMERGENT_MISCONCEPTIONS", "1")
    sess, attempt = await _mk(db)

    calls = []

    async def spy(db, *, search_space_id, concept_id, user_id, attempt_id, canonical_payload):
        calls.append(
            {
                "search_space_id": search_space_id,
                "concept_id": concept_id,
                "user_id": user_id,
                "attempt_id": attempt_id,
                "grader_used": canonical_payload["grader_used"],
            }
        )
        return 0

    monkeypatch.setattr("apollo.handlers.artifact_writer.record_observations_from_canonical", spy)
    await _run(db, sess, attempt)

    assert len(calls) == 1
    assert calls[0] == {
        "search_space_id": 1,
        "concept_id": 42,
        "user_id": _USER,
        "attempt_id": int(attempt.id),
        "grader_used": "llm_fallback",
    }


@pytest.mark.asyncio
async def test_flag_on_store_failure_is_swallowed(db, monkeypatch):
    """Flag ON: a store-write failure is logged + rolled back but NEVER raised —
    the already-committed grade survives and the canonical payload is returned."""
    monkeypatch.setenv("APOLLO_EMERGENT_MISCONCEPTIONS", "1")
    sess, attempt = await _mk(db)

    async def boom(*a, **k):
        raise RuntimeError("ledger down")

    monkeypatch.setattr("apollo.handlers.artifact_writer.record_observations_from_canonical", boom)
    payload = await _run(db, sess, attempt)

    assert payload is not None  # grade unaffected
    # The canonical artifact remains committed despite the store failure.
    assert (await db.execute(select(func.count()).select_from(GradingArtifact))).scalar_one() == 1
