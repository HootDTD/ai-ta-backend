"""Capture-seam tests (2026-07-10 emergent misconception map plan, Wave 2).

``record_detector_births`` (T2) is the write side of the detector-unkeyed
birth signal: it maps each qualifying ``collect_unkeyed_births`` finding's
``concept_key`` (a reference-graph ``node_id``) to its ``entity_key`` via a
caller-supplied map, and appends a ``source='detector_unkeyed'`` observation
via ``apollo.emergent.store.record_observation`` — the same idempotent insert
core used by the existing write paths.

``record_clarification_refuted`` (T3) is the write side of the
clarification-refuted upgrade signal: the caller (``resolve_turn.py``) has
already resolved ``signature``/``opposes`` from the refuted candidate/
clarification row (R2 — see ``test_resolve_turn.py`` for the exact mapping
proof); this function is a thin, single-purpose wrapper over
``record_observation`` with ``source='clarification_refuted'``.

In-memory SQLite harness, mirroring ``apollo/emergent/tests/test_store.py``.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.emergent import capture
from apollo.overseer.misconception_detector.types import ConceptFinding
from apollo.persistence.models import MisconceptionObservation
from database.models import Base

_SPACE = 1
_CONCEPT = 7
_USER = "a0000000-0000-4000-8000-000000000001"


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sc: Base.metadata.create_all(sc, tables=[MisconceptionObservation.__table__])
        )
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as s:
        yield s
    await engine.dispose()


async def _count(db: AsyncSession) -> int:
    return int(
        (await db.execute(select(func.count()).select_from(MisconceptionObservation))).scalar_one()
    )


def _finding(
    *,
    concept_key: str = "node.real_basis",
    verdict: str = "wrong",
    confidence: float = 0.95,
    evidence_span: str = "student said real GDP includes inflation",
) -> ConceptFinding:
    return ConceptFinding(
        concept_key=concept_key,
        verdict=verdict,  # type: ignore[arg-type]
        confidence=confidence,
        severity=0.0,
        evidence_span=evidence_span,
        signature=f"unkeyed:{concept_key}",
        source="judge",
        corroborated=False,
        bank_code=None,
    )


@pytest.mark.asyncio
async def test_record_detector_births_writes_one_observation(db):
    birth = _finding()
    n = await capture.record_detector_births(
        db,
        search_space_id=_SPACE,
        concept_id=_CONCEPT,
        user_id=_USER,
        attempt_id=900,
        births=(birth,),
        node_entity_key={"node.real_basis": "def.real_basis"},
    )
    await db.commit()

    assert n == 1
    row = (await db.execute(select(MisconceptionObservation))).scalars().one()
    assert row.signature == "emergent.def.real_basis"
    assert row.opposes == "def.real_basis"
    assert row.confidence == pytest.approx(0.95)
    assert row.evidence_span == birth.evidence_span
    assert row.source == "detector_unkeyed"
    assert row.attempt_id == 900
    assert row.search_space_id == _SPACE
    assert row.concept_id == _CONCEPT
    assert row.user_id == _USER


@pytest.mark.asyncio
async def test_record_detector_births_skips_finding_without_entity_key(db):
    """Spec §5.2 scope boundary: a finding at a node with no entity_key has no
    stable signature and is NOT captured."""
    birth = _finding(concept_key="node.unmapped")
    n = await capture.record_detector_births(
        db,
        search_space_id=_SPACE,
        concept_id=_CONCEPT,
        user_id=_USER,
        attempt_id=901,
        births=(birth,),
        node_entity_key={},  # no entry for node.unmapped
    )
    await db.commit()

    assert n == 0
    assert await _count(db) == 0


@pytest.mark.asyncio
async def test_record_detector_births_writes_only_mapped_findings(db):
    """A mix of mapped and unmapped births: only the mapped one is written."""
    mapped = _finding(concept_key="node.real_basis")
    unmapped = _finding(concept_key="node.unmapped", evidence_span="other span")
    n = await capture.record_detector_births(
        db,
        search_space_id=_SPACE,
        concept_id=_CONCEPT,
        user_id=_USER,
        attempt_id=902,
        births=(mapped, unmapped),
        node_entity_key={"node.real_basis": "def.real_basis"},
    )
    await db.commit()

    assert n == 1
    row = (await db.execute(select(MisconceptionObservation))).scalars().one()
    assert row.signature == "emergent.def.real_basis"


@pytest.mark.asyncio
async def test_record_detector_births_idempotent_on_recall(db):
    birth = _finding()
    kwargs = dict(
        search_space_id=_SPACE,
        concept_id=_CONCEPT,
        user_id=_USER,
        attempt_id=903,
        births=(birth,),
        node_entity_key={"node.real_basis": "def.real_basis"},
    )
    first = await capture.record_detector_births(db, **kwargs)
    await db.commit()
    second = await capture.record_detector_births(db, **kwargs)
    await db.commit()

    assert first == 1
    assert second == 0
    assert await _count(db) == 1


@pytest.mark.asyncio
async def test_record_detector_births_empty_births_writes_nothing(db):
    n = await capture.record_detector_births(
        db,
        search_space_id=_SPACE,
        concept_id=_CONCEPT,
        user_id=_USER,
        attempt_id=904,
        births=(),
        node_entity_key={},
    )
    await db.commit()

    assert n == 0
    assert await _count(db) == 0


@pytest.mark.asyncio
async def test_record_detector_births_distinct_concepts_write_distinct_signatures(db):
    b1 = _finding(concept_key="node.a")
    b2 = _finding(concept_key="node.b", evidence_span="other")
    n = await capture.record_detector_births(
        db,
        search_space_id=_SPACE,
        concept_id=_CONCEPT,
        user_id=_USER,
        attempt_id=905,
        births=(b1, b2),
        node_entity_key={"node.a": "def.a", "node.b": "eq.b"},
    )
    await db.commit()

    assert n == 2
    sigs = {
        r.signature for r in (await db.execute(select(MisconceptionObservation))).scalars().all()
    }
    assert sigs == {"emergent.def.a", "emergent.eq.b"}


# --------------------------------------------------------------------------- #
# record_clarification_refuted (T3, spec §5.3.2)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_record_clarification_refuted_writes_one_observation(db):
    n = await capture.record_clarification_refuted(
        db,
        search_space_id=_SPACE,
        concept_id=_CONCEPT,
        user_id=_USER,
        attempt_id=910,
        signature="emergent.def.real_basis",
        opposes="def.real_basis",
        confidence=0.8,
        evidence_span="no, real GDP includes price changes",
    )
    await db.commit()

    assert n == 1
    row = (await db.execute(select(MisconceptionObservation))).scalars().one()
    assert row.signature == "emergent.def.real_basis"
    assert row.opposes == "def.real_basis"
    assert row.confidence == pytest.approx(0.8)
    assert row.evidence_span == "no, real GDP includes price changes"
    assert row.source == "clarification_refuted"
    assert row.attempt_id == 910


@pytest.mark.asyncio
async def test_record_clarification_refuted_idempotent_on_recall(db):
    kwargs = dict(
        search_space_id=_SPACE,
        concept_id=_CONCEPT,
        user_id=_USER,
        attempt_id=911,
        signature="emergent.eq.newton2",
        opposes="eq.newton2",
        confidence=0.6,
        evidence_span="span",
    )
    first = await capture.record_clarification_refuted(db, **kwargs)
    await db.commit()
    second = await capture.record_clarification_refuted(db, **kwargs)
    await db.commit()

    assert first == 1
    assert second == 0
    assert await _count(db) == 1


@pytest.mark.asyncio
async def test_record_clarification_refuted_propagates_store_errors(db):
    """The capture function itself does no failure-swallowing — that is the
    CALLER's own-failure-domain job (resolve_turn.py). A bad source would be
    a programmer error here since 'clarification_refuted' is hardcoded, but a
    store-level failure (e.g. a DB error) must propagate uncaught so the
    caller's try/except can see it."""
    import apollo.emergent.capture as capture_mod

    async def _boom(*_a, **_kw):
        raise RuntimeError("db exploded")

    original = capture_mod.record_observation
    capture_mod.record_observation = _boom  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError, match="db exploded"):
            await capture.record_clarification_refuted(
                db,
                search_space_id=_SPACE,
                concept_id=_CONCEPT,
                user_id=_USER,
                attempt_id=912,
                signature="emergent.def.x",
                opposes="def.x",
                confidence=0.5,
                evidence_span="span",
            )
    finally:
        capture_mod.record_observation = original
