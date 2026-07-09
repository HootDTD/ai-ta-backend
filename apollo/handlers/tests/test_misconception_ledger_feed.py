"""T14 — emergent-ledger feed assertion (both flags ON).

Frozen contract (2026-07-08 misconception-detector plan, T14 / plan §6.5 / A5):
with ``APOLLO_MISCONCEPTION_DETECTOR`` and ``APOLLO_EMERGENT_MISCONCEPTIONS``
both ON, a gate-cleared KEYED detection (``merge.py``'s ``MergeOutcome``
carrying a ``misc.<code>`` row) must reach ``apollo_misconception_observations``
via the ALREADY-WIRED path:

    write_artifacts(detection_outcome=...)
      -> build_llm_artifact(detection_outcome=...)     [T11]
      -> canonical_payload["misconceptions"] = [{canonical_key: "misc.<code>", ...}]
      -> record_observations_from_canonical(canonical_payload)  [T12, gated by
         emergent_misconceptions_enabled()]
      -> apollo_misconception_observations row, signature == "misc.<code>"  (A5)

No new production module is added here — T7 (``merge.py``), T11
(``artifact_build.py``), and T12 (``artifact_writer.py``) already wire this
path end to end; this file is the missing assertion that the ROW actually
lands with the bare, non-double-prefixed signature, and that a docked
UNKEYED finding contributes to the penalty but never creates a keyed/
double-prefixed ledger row (A5's other half).

Fully offline: in-memory SQLite (``sqlite+aiosqlite:///:memory:``), zero
network, zero real OpenAI/Neo4j — mirrors
``test_artifact_writer_emergent.py`` / ``test_artifact_writer_misconception.py``'s
harness. No Docker/Postgres portion is exercised here (the pgvector-only
paths are out of scope for this seam); if a real-Postgres row-write needs
separate coverage later, that is a Testcontainers/local-Docker addition, not
this file's job.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.handlers import artifact_writer
from apollo.overseer.misconception_detector.types import ConceptFinding, MergeOutcome
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
_CONCEPT_ID = 42

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


async def _mk(db, *, concept_id: int | None = _CONCEPT_ID):
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


def _keyed_outcome() -> MergeOutcome:
    """A gate-cleared outcome carrying ONE bank-keyed row (A5: bare
    ``misc.<code>``, exactly as ``merge.py::_keyed_row`` emits it)."""
    return MergeOutcome(
        misconception_penalty=0.18,
        misconceptions=(
            {
                "canonical_key": "misc.includes_transfers",
                "evidence_span": "GDP includes transfer payments",
                "confidence": 0.91,
                "opposes": "eq.gdp_identity",
            },
        ),
        ceiling_applied=False,
        ledger_findings=(),
    )


def _unkeyed_only_outcome() -> MergeOutcome:
    """A docked outcome whose penalty is non-zero (an unkeyed finding still
    docks per merge.py) but whose ``misconceptions[]`` is EMPTY — mirroring
    ``merge.py``'s A5 exclusion of ``unkeyed:<concept_id>`` signatures from the
    keyed row list. This is the shape the store actually receives for an
    unkeyed-only detection: no row for the store to key on except the
    per-concept unkeyed bucket via ``node_ledger`` (not exercised by the LLM
    artifact builder), so with ONLY ``misconceptions=()`` the store must write
    ZERO rows — proving no double-prefixed ``unkeyed:unkeyed:...`` or
    mis-keyed row is ever created for a signature that never had a
    ``canonical_key`` to begin with."""
    return MergeOutcome(
        misconception_penalty=0.09,
        misconceptions=(),
        ceiling_applied=False,
        ledger_findings=(),
    )


async def _run(db, sess, attempt, *, detection_outcome):
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


async def _observations(db) -> list[MisconceptionObservation]:
    return list((await db.execute(select(MisconceptionObservation))).scalars().all())


@pytest.mark.asyncio
async def test_both_flags_on_keyed_detection_writes_bare_signature_row(db, monkeypatch):
    """Core T14 assertion: both flags ON + a gate-cleared KEYED detection ->
    one ``apollo_misconception_observations`` row whose ``signature`` is the
    BARE ``misc.<code>`` (A5) — never re-prefixed, never ``unkeyed:*``."""
    monkeypatch.setenv("APOLLO_MISCONCEPTION_DETECTOR", "1")
    monkeypatch.setenv("APOLLO_EMERGENT_MISCONCEPTIONS", "1")
    sess, attempt = await _mk(db)

    payload = await _run(db, sess, attempt, detection_outcome=_keyed_outcome())

    assert payload is not None
    assert payload["misconceptions"] == [
        {
            "canonical_key": "misc.includes_transfers",
            "evidence_span": "GDP includes transfer payments",
            "confidence": 0.91,
            "opposes": "eq.gdp_identity",
        }
    ]

    rows = await _observations(db)
    assert len(rows) == 1
    row = rows[0]
    assert row.signature == "misc.includes_transfers"
    assert not row.signature.startswith("unkeyed:")
    assert not row.signature.startswith("misc.misc.")
    assert row.attempt_id == int(attempt.id)
    assert row.search_space_id == 1
    assert row.concept_id == _CONCEPT_ID
    assert row.user_id == _USER
    assert row.confidence == pytest.approx(0.91)
    assert row.opposes == "eq.gdp_identity"


@pytest.mark.asyncio
async def test_unkeyed_only_detection_writes_no_double_prefixed_row(db, monkeypatch):
    """A docked-but-unkeyed-only outcome (``misconceptions=()``, A5's merge.py
    exclusion) must NOT create any ledger row — in particular no
    ``unkeyed:unkeyed:...`` double-prefixed row and no keyed row conjured out
    of nothing. The penalty still applied to the served grade (proving this
    is a real, non-empty outcome) even though the ledger stays untouched."""
    monkeypatch.setenv("APOLLO_MISCONCEPTION_DETECTOR", "1")
    monkeypatch.setenv("APOLLO_EMERGENT_MISCONCEPTIONS", "1")
    sess, attempt = await _mk(db)

    payload = await _run(db, sess, attempt, detection_outcome=_unkeyed_only_outcome())

    assert payload is not None
    assert payload["scores"]["misconception_penalty"] == pytest.approx(0.09)
    assert payload["misconceptions"] == []

    rows = await _observations(db)
    assert rows == []
    assert not any(r.signature.startswith("unkeyed:unkeyed:") for r in rows)


@pytest.mark.asyncio
async def test_idempotent_regrade_of_same_attempt_adds_zero_rows(db, monkeypatch):
    """T14 spec assertion: 'idempotent re-grade adds 0.' Re-running
    ``write_artifacts`` for the SAME attempt with the same keyed outcome must
    not create a second observation row (``ON CONFLICT (attempt_id,
    signature) DO NOTHING`` in ``store.py``)."""
    monkeypatch.setenv("APOLLO_MISCONCEPTION_DETECTOR", "1")
    monkeypatch.setenv("APOLLO_EMERGENT_MISCONCEPTIONS", "1")
    sess, attempt = await _mk(db)

    await _run(db, sess, attempt, detection_outcome=_keyed_outcome())
    rows_after_first = await _observations(db)
    assert len(rows_after_first) == 1

    # Re-grade the SAME attempt (e.g. a retry/re-click of Done). GradingArtifact
    # has a UNIQUE(attempt_id, role) constraint, so clear the prior canonical
    # row first to isolate the ledger's OWN idempotency guard rather than
    # tripping the artifact table's constraint.
    await db.execute(GradingArtifact.__table__.delete())
    await db.commit()

    await _run(db, sess, attempt, detection_outcome=_keyed_outcome())
    rows_after_second = await _observations(db)

    assert len(rows_after_second) == 1
    assert rows_after_second[0].signature == "misc.includes_transfers"


def _cokeyed_judge_bank_outcome() -> MergeOutcome:
    """A gate-cleared outcome modeling the corroboration/keying redesign's
    row-3 dock (docs/_archive/specs/2026-07-08-apollo-misconception-
    corroboration-redesign.md §4.4/§5): a judge finding co-keyed with a
    bank_pattern finding on the same validated ``misc.<code>``, docked under
    the JUDGE's (node_id-shaped) concept_key, ceiling-eligible. This is the
    keyed-ledger-row observable the two prior validation runs could never
    produce because judge signatures were always ``unkeyed:*`` before A11."""
    docked = ConceptFinding(
        concept_key="node.demand_curve",
        verdict="misconception",
        confidence=0.91,
        severity=0.27,
        evidence_span="GDP includes transfer payments",
        signature="misc.includes_transfers",
        source="judge",
        corroborated=True,
        bank_code="includes_transfers",
        bank_match_above_floor=True,
        ceiling_eligible=True,
    )
    return MergeOutcome(
        misconception_penalty=0.27,
        misconceptions=(
            {
                "canonical_key": "misc.includes_transfers",
                "evidence_span": "GDP includes transfer payments",
                "confidence": 0.91,
                "opposes": None,
            },
        ),
        ceiling_applied=True,
        ledger_findings=(docked,),
    )


@pytest.mark.asyncio
async def test_cokeyed_judge_bank_dock_writes_keyed_row(db, monkeypatch):
    """A9-A13 redesign: a co-keyed judge+bank dock (row 3 of the gate
    truth-table) now writes a NON-ZERO keyed row to
    ``apollo_misconception_observations`` — the observable the two prior
    validation runs could never satisfy because judge signatures were always
    unkeyed. Both flags ON."""
    monkeypatch.setenv("APOLLO_MISCONCEPTION_DETECTOR", "1")
    monkeypatch.setenv("APOLLO_EMERGENT_MISCONCEPTIONS", "1")
    sess, attempt = await _mk(db)

    payload = await _run(db, sess, attempt, detection_outcome=_cokeyed_judge_bank_outcome())

    assert payload is not None
    assert payload["misconceptions"] == [
        {
            "canonical_key": "misc.includes_transfers",
            "evidence_span": "GDP includes transfer payments",
            "confidence": 0.91,
            "opposes": None,
        }
    ]

    rows = await _observations(db)
    assert len(rows) == 1
    row = rows[0]
    assert row.signature == "misc.includes_transfers"
    assert not row.signature.startswith("unkeyed:")
    assert row.confidence == pytest.approx(0.91)


@pytest.mark.asyncio
async def test_either_flag_off_writes_no_ledger_rows(db, monkeypatch):
    """Sanity/regression bookend: with the EMERGENT flag OFF (even though the
    detector flag is ON and a keyed outcome is threaded through), the store
    never runs — zero rows, matching ``emergent_misconceptions_enabled()``'s
    dormancy contract."""
    monkeypatch.setenv("APOLLO_MISCONCEPTION_DETECTOR", "1")
    monkeypatch.delenv("APOLLO_EMERGENT_MISCONCEPTIONS", raising=False)
    sess, attempt = await _mk(db)

    payload = await _run(db, sess, attempt, detection_outcome=_keyed_outcome())

    assert payload is not None
    assert payload["misconceptions"] != []  # the grade itself still carries it
    assert await _observations(db) == []
