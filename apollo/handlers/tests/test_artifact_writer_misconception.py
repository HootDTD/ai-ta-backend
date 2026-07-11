"""T12 — ``write_artifacts`` threads ``detection_outcome`` into
``build_llm_artifact``.

Frozen contract (2026-07-08 misconception-detector plan, T12):
``write_artifacts`` grows a NEW ``detection_outcome: MergeOutcome | None =
None`` keyword, forwarded into ``build_llm_artifact``. Default ``None`` keeps
every existing caller/test byte-identical (design invariant #1) — proven here
by re-running the SAME emergent-store harness pattern
(``test_artifact_writer_emergent.py``: in-memory sqlite, no Docker, no
network) with and without the new kwarg and asserting identical payloads.

A NON-empty outcome must reach the persisted ``misconceptions`` column (via
``build_llm_artifact``'s ``has_detection`` branch) — proving the emergent
writer path (``record_observations_from_canonical``, gated by
``APOLLO_EMERGENT_MISCONCEPTIONS``) now has real rows to feed once BOTH flags
are on, with no change to ``store.py`` (spec §6.5).
"""

from __future__ import annotations

import inspect
import json

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.handlers import artifact_writer
from apollo.overseer.misconception_detector.types import MergeOutcome
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

_COVERAGE = {"per_step": {"n1": "covered", "n2": "missing"}, "confidences": {"n1": 0.9}}
_RUBRIC = {"overall": {"score": 80, "letter": "B"}}


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


async def _mk(db, *, concept_id: int | None = None):
    sess = ApolloSession(
        user_id=_USER,
        search_space_id=1,
        concept_id=concept_id,
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


def _non_empty_outcome() -> MergeOutcome:
    return MergeOutcome(
        misconception_penalty=0.15,
        misconceptions=(
            {
                "canonical_key": "misc.includes_transfers",
                "evidence_span": "GDP includes transfer payments",
                "confidence": 0.92,
                "opposes": "eq.gdp_identity",
            },
        ),
        ceiling_applied=False,
        ledger_findings=(),
    )


def _empty_outcome() -> MergeOutcome:
    return MergeOutcome(
        misconception_penalty=0.0,
        misconceptions=(),
        ceiling_applied=False,
        ledger_findings=(),
    )


async def _run(db, sess, attempt, *, detection_outcome=None):
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
        detection_outcome=detection_outcome,
    )


def test_write_artifacts_accepts_detection_outcome_kwarg():
    """Signature-level RED: ``write_artifacts`` must expose a
    ``detection_outcome`` keyword defaulting to ``None``."""
    sig = inspect.signature(artifact_writer.write_artifacts)
    assert "detection_outcome" in sig.parameters
    assert sig.parameters["detection_outcome"].default is None


@pytest.mark.asyncio
async def test_default_none_is_byte_identical_to_pre_change_callers(db, monkeypatch):
    """Existing callers that never pass ``detection_outcome`` (every caller
    before T12/T13 land) must see a BYTE-IDENTICAL canonical payload — proving
    the new kwarg is a true opt-in with no behavior change on the default
    path (design invariant #1)."""
    monkeypatch.delenv("APOLLO_EMERGENT_MISCONCEPTIONS", raising=False)
    sess, attempt = await _mk(db)

    payload_omitted = await artifact_writer.write_artifacts(
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

    await db.execute(GradingArtifact.__table__.delete())
    await db.commit()

    payload_explicit_none = await artifact_writer.write_artifacts(
        db,
        attempt=attempt,
        sess=sess,
        shadow=None,
        coverage=_COVERAGE,
        rubric=_RUBRIC,
        served="llm_fallback",
        graph_failure=None,
        latency_ms=123,
        detection_outcome=None,
    )

    assert json.dumps(payload_omitted, sort_keys=True) == json.dumps(
        payload_explicit_none, sort_keys=True
    )
    assert payload_omitted["scores"]["misconception_penalty"] == 0.0
    assert payload_omitted["misconceptions"] == []


@pytest.mark.asyncio
async def test_empty_outcome_is_byte_identical_to_none(db):
    """An EMPTY ``MergeOutcome`` (penalty 0.0, no rows) is a no-op — same
    payload as passing ``None`` outright (the flag-OFF/found-nothing
    regression guard, spec design invariant #1)."""
    sess, attempt = await _mk(db)

    payload_none = await _run(db, sess, attempt, detection_outcome=None)

    await db.execute(GradingArtifact.__table__.delete())
    await db.commit()

    payload_empty = await _run(db, sess, attempt, detection_outcome=_empty_outcome())

    assert json.dumps(payload_none, sort_keys=True) == json.dumps(payload_empty, sort_keys=True)


@pytest.mark.asyncio
async def test_non_empty_outcome_threaded_into_persisted_misconceptions(db):
    """A NON-empty outcome must reach ``build_llm_artifact`` and land on the
    persisted ``misconceptions`` column — the core T12 wiring assertion."""
    sess, attempt = await _mk(db)

    payload = await _run(db, sess, attempt, detection_outcome=_non_empty_outcome())

    assert payload is not None
    assert payload["scores"]["misconception_penalty"] == pytest.approx(0.15)
    assert payload["misconceptions"] == [
        {
            "canonical_key": "misc.includes_transfers",
            "evidence_span": "GDP includes transfer payments",
            "confidence": 0.92,
            "opposes": "eq.gdp_identity",
        }
    ]

    row = (await db.execute(GradingArtifact.__table__.select())).mappings().one()
    assert row["misconceptions"] == payload["misconceptions"]


@pytest.mark.asyncio
async def test_emergent_writer_path_picks_up_populated_misconceptions(db, monkeypatch):
    """Spec §6.5: with the emergent-store flag ON, the EXISTING
    ``record_observations_from_canonical`` call (no change to ``store.py``)
    now receives a canonical payload whose ``misconceptions[]`` is populated
    by the threaded ``detection_outcome`` — proving the emergent ledger feed
    picks up the now-real rows with zero changes to the store call site."""
    monkeypatch.setenv("APOLLO_EMERGENT_MISCONCEPTIONS", "1")
    sess, attempt = await _mk(db)

    calls = []

    async def spy(db, *, search_space_id, concept_id, user_id, attempt_id, canonical_payload):
        calls.append(canonical_payload)
        return 0

    monkeypatch.setattr("apollo.handlers.artifact_writer.record_observations_from_canonical", spy)

    payload = await _run(db, sess, attempt, detection_outcome=_non_empty_outcome())

    assert payload is not None
    assert len(calls) == 1
    assert calls[0]["misconceptions"] == [
        {
            "canonical_key": "misc.includes_transfers",
            "evidence_span": "GDP includes transfer payments",
            "confidence": 0.92,
            "opposes": "eq.gdp_identity",
        }
    ]


@pytest.mark.asyncio
async def test_graph_served_path_unaffected_by_detection_outcome(db):
    """``detection_outcome`` only feeds the LLM-fallback builder (spec §6.3);
    threading it must not disturb the graph-served path when there is no
    shadow (defensive branch falls back to the LLM payload, which DOES pick
    up the outcome — this asserts that fallback still happens cleanly with
    the new kwarg present)."""
    sess, attempt = await _mk(db)

    payload = await artifact_writer.write_artifacts(
        db,
        attempt=attempt,
        sess=sess,
        shadow=None,
        coverage=_COVERAGE,
        rubric=_RUBRIC,
        served="graph",
        graph_failure=None,
        latency_ms=None,
        detection_outcome=_non_empty_outcome(),
    )
    assert payload is not None
    assert payload["grader_used"] == "llm_fallback"
    assert payload["scores"]["misconception_penalty"] == pytest.approx(0.15)
