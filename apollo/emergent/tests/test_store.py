"""Store write + derived-on-read tests (in-memory SQLite; FKs not enforced).

Covers write-path idempotency, the unkeyed bucket, per-signature aggregation,
and the promoted-dict read shape (keyed + promoted only, unkeyed excluded).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from apollo.emergent import store
from apollo.persistence.models import MisconceptionObservation
from database.models import Base

_SPACE = 1
_CONCEPT = 7
_U1 = "a0000000-0000-4000-8000-000000000001"
_U2 = "b0000000-0000-4000-8000-000000000002"
_U3 = "c0000000-0000-4000-8000-000000000003"
_NOW = datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC)


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


def _payload(misconceptions=None, node_ledger=None) -> dict:
    return {
        "misconceptions": misconceptions or [],
        "node_ledger": node_ledger or [],
    }


async def _count(db: AsyncSession) -> int:
    return int(
        (await db.execute(select(func.count()).select_from(MisconceptionObservation))).scalar_one()
    )


@pytest.mark.asyncio
async def test_write_derives_keyed_observation(db):
    payload = _payload(
        misconceptions=[
            {
                "canonical_key": "misc.sign_error",
                "evidence_span": "flipped the sign",
                "confidence": 0.9,
                "opposes": "eq.newton2",
            }
        ]
    )
    n = await store.record_observations_from_canonical(
        db,
        search_space_id=_SPACE,
        concept_id=_CONCEPT,
        user_id=_U1,
        attempt_id=101,
        canonical_payload=payload,
    )
    await db.commit()
    assert n == 1
    row = (await db.execute(select(MisconceptionObservation))).scalars().one()
    assert row.signature == "misc.sign_error"
    assert row.confidence == pytest.approx(0.9)
    assert row.opposes == "eq.newton2"
    assert row.evidence_span == "flipped the sign"
    assert row.source == "grading_artifact"


@pytest.mark.asyncio
async def test_write_is_idempotent_per_attempt(db):
    payload = _payload(
        misconceptions=[
            {"canonical_key": "misc.a", "confidence": 0.8, "opposes": None, "evidence_span": "x"}
        ]
    )
    first = await store.record_observations_from_canonical(
        db,
        search_space_id=_SPACE,
        concept_id=_CONCEPT,
        user_id=_U1,
        attempt_id=200,
        canonical_payload=payload,
    )
    await db.commit()
    second = await store.record_observations_from_canonical(
        db,
        search_space_id=_SPACE,
        concept_id=_CONCEPT,
        user_id=_U1,
        attempt_id=200,
        canonical_payload=payload,
    )
    await db.commit()
    assert first == 1
    assert second == 0  # ON CONFLICT (attempt_id, signature) DO NOTHING
    assert await _count(db) == 1


@pytest.mark.asyncio
async def test_free_form_goes_to_unkeyed_bucket(db):
    payload = _payload(
        misconceptions=[
            {
                "canonical_key": None,
                "confidence": 0.7,
                "opposes": None,
                "evidence_span": "vague wrong idea",
            }
        ]
    )
    await store.record_observations_from_canonical(
        db,
        search_space_id=_SPACE,
        concept_id=_CONCEPT,
        user_id=_U1,
        attempt_id=300,
        canonical_payload=payload,
    )
    await db.commit()
    row = (await db.execute(select(MisconceptionObservation))).scalars().one()
    assert row.signature == f"unkeyed:{_CONCEPT}"


@pytest.mark.asyncio
async def test_free_form_with_null_concept_uses_none_bucket(db):
    payload = _payload(
        misconceptions=[
            {"canonical_key": None, "confidence": 0.7, "opposes": None, "evidence_span": "vague"}
        ]
    )
    await store.record_observations_from_canonical(
        db,
        search_space_id=_SPACE,
        concept_id=None,
        user_id=_U1,
        attempt_id=301,
        canonical_payload=payload,
    )
    await db.commit()
    row = (await db.execute(select(MisconceptionObservation))).scalars().one()
    assert row.signature == "unkeyed:none"


@pytest.mark.asyncio
async def test_node_ledger_misconception_feeds_store_and_dedups(db):
    # Same signature appears in misconceptions[] AND node_ledger[] -> one row.
    payload = _payload(
        misconceptions=[
            {
                "canonical_key": "misc.dup",
                "confidence": 0.9,
                "opposes": "eq.x",
                "evidence_span": "span-a",
            }
        ],
        node_ledger=[
            {
                "canonical_key": "misc.dup",
                "status": "misconception",
                "confidence": 0.9,
                "evidence_span": "span-a",
            },
            {
                "canonical_key": "misc.only_ledger",
                "status": "misconception",
                "confidence": 0.6,
                "evidence_span": "span-b",
            },
            {
                "canonical_key": "eq.credited",
                "status": "credited",
                "confidence": 1.0,
                "evidence_span": "ok",
            },
        ],
    )
    n = await store.record_observations_from_canonical(
        db,
        search_space_id=_SPACE,
        concept_id=_CONCEPT,
        user_id=_U1,
        attempt_id=400,
        canonical_payload=payload,
    )
    await db.commit()
    sigs = {
        r.signature for r in (await db.execute(select(MisconceptionObservation))).scalars().all()
    }
    assert n == 2
    assert sigs == {"misc.dup", "misc.only_ledger"}  # credited never enters


@pytest.mark.asyncio
async def test_duplicate_signature_within_misconceptions_deduped(db):
    # Two misconceptions[] entries with the same key in ONE artifact -> one row.
    payload = _payload(
        misconceptions=[
            {
                "canonical_key": "misc.same",
                "confidence": 0.9,
                "opposes": "eq.x",
                "evidence_span": "first",
            },
            {
                "canonical_key": "misc.same",
                "confidence": 0.5,
                "opposes": "eq.x",
                "evidence_span": "second",
            },
        ]
    )
    n = await store.record_observations_from_canonical(
        db,
        search_space_id=_SPACE,
        concept_id=_CONCEPT,
        user_id=_U1,
        attempt_id=350,
        canonical_payload=payload,
    )
    await db.commit()
    assert n == 1
    row = (await db.execute(select(MisconceptionObservation))).scalars().one()
    assert row.evidence_span == "first"  # first entry wins the dedup


async def _seed(db, *, attempt_id, user_id, key, conf, opposes="eq.x", span="s", created=None):
    row = MisconceptionObservation(
        search_space_id=_SPACE,
        concept_id=_CONCEPT,
        signature=key,
        user_id=user_id,
        attempt_id=attempt_id,
        confidence=conf,
        opposes=opposes,
        evidence_span=span,
        source="grading_artifact",
        created_at=created or _NOW,
    )
    db.add(row)
    await db.flush()


@pytest.mark.asyncio
async def test_aggregate_counts_distinct_students_and_mean_confidence(db):
    await _seed(db, attempt_id=1, user_id=_U1, key="misc.k", conf=1.0, span="s1")
    await _seed(db, attempt_id=2, user_id=_U2, key="misc.k", conf=0.6, span="s2")
    await _seed(
        db, attempt_id=3, user_id=_U1, key="misc.k", conf=0.8, span="s1"
    )  # dup student/span
    await db.commit()
    aggs = await store.aggregate_signatures(db, search_space_id=_SPACE, concept_id=_CONCEPT)
    assert len(aggs) == 1
    a = aggs[0]
    assert a.signature == "misc.k"
    assert a.observation_count == 3
    assert a.distinct_students == 2
    assert a.mean_confidence == pytest.approx((1.0 + 0.6 + 0.8) / 3)
    assert set(a.evidence_spans) == {"s1", "s2"}


@pytest.mark.asyncio
async def test_aggregate_empty_for_none_concept(db):
    await _seed(db, attempt_id=1, user_id=_U1, key="misc.k", conf=1.0)
    await db.commit()
    assert await store.aggregate_signatures(db, search_space_id=_SPACE, concept_id=None) == []


@pytest.mark.asyncio
async def test_promoted_dict_returns_keyed_promoted_only(db):
    # misc.hot: 3 distinct students, conf 1.0, recent -> trust 1.0 -> promoted.
    for i, u in enumerate((_U1, _U2, _U3)):
        await _seed(
            db,
            attempt_id=10 + i,
            user_id=u,
            key="misc.hot",
            conf=1.0,
            opposes="eq.hot",
            span=f"hot-{i}",
        )
    # misc.cold: 1 student -> trust ~0.33 -> NOT promoted.
    await _seed(db, attempt_id=20, user_id=_U1, key="misc.cold", conf=1.0)
    # unkeyed bucket at max support -> NEVER promoted.
    for i, u in enumerate((_U1, _U2, _U3)):
        await _seed(
            db,
            attempt_id=30 + i,
            user_id=u,
            key=f"unkeyed:{_CONCEPT}",
            conf=1.0,
            opposes=None,
            span="free",
        )
    await db.commit()

    result = await store.load_promoted_misconceptions_dict(
        db, search_space_id=_SPACE, concept_id=_CONCEPT, now=_NOW
    )
    keys = {m["key"] for m in result["misconceptions"]}
    assert keys == {"misc.hot"}
    hot = result["misconceptions"][0]
    assert hot["opposes"] == "eq.hot"
    assert hot["display_name"] == "misc.hot"
    assert set(hot["trigger_phrases"]) == {"hot-0", "hot-1", "hot-2"}


@pytest.mark.asyncio
async def test_promoted_demoted_by_recency(db):
    old = _NOW - timedelta(days=90)
    for i, u in enumerate((_U1, _U2, _U3)):
        await _seed(db, attempt_id=40 + i, user_id=u, key="misc.stale", conf=1.0, created=old)
    await db.commit()
    result = await store.load_promoted_misconceptions_dict(
        db, search_space_id=_SPACE, concept_id=_CONCEPT, now=_NOW
    )
    assert result["misconceptions"] == []


@pytest.mark.asyncio
async def test_record_observation_inserts_one_row(db):
    n = await store.record_observation(
        db,
        search_space_id=_SPACE,
        concept_id=_CONCEPT,
        user_id=_U1,
        attempt_id=500,
        signature="emergent.def.real_basis",
        confidence=0.7,
        opposes="def.real_basis",
        evidence_span="student said real GDP includes inflation",
        source="detector_unkeyed",
    )
    await db.commit()
    assert n == 1
    row = (await db.execute(select(MisconceptionObservation))).scalars().one()
    assert row.signature == "emergent.def.real_basis"
    assert row.confidence == pytest.approx(0.7)
    assert row.opposes == "def.real_basis"
    assert row.evidence_span == "student said real GDP includes inflation"
    assert row.source == "detector_unkeyed"


@pytest.mark.asyncio
async def test_record_observation_is_idempotent_on_attempt_signature(db):
    kwargs = dict(
        search_space_id=_SPACE,
        concept_id=_CONCEPT,
        user_id=_U1,
        attempt_id=501,
        signature="emergent.eq.newton2",
        confidence=0.6,
        opposes="eq.newton2",
        evidence_span="span",
        source="clarification_refuted",
    )
    first = await store.record_observation(db, **kwargs)
    await db.commit()
    second = await store.record_observation(db, **kwargs)
    await db.commit()
    assert first == 1
    assert second == 0
    assert await _count(db) == 1


@pytest.mark.asyncio
async def test_record_observation_rejects_out_of_domain_source(db):
    with pytest.raises(ValueError, match="source"):
        await store.record_observation(
            db,
            search_space_id=_SPACE,
            concept_id=_CONCEPT,
            user_id=_U1,
            attempt_id=502,
            signature="emergent.def.x",
            confidence=0.5,
            opposes="def.x",
            evidence_span="span",
            source="bogus",
        )
    assert await _count(db) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["grading_artifact", "detector_unkeyed", "clarification_refuted"])
async def test_record_observation_accepts_every_domain_value(db, source):
    n = await store.record_observation(
        db,
        search_space_id=_SPACE,
        concept_id=_CONCEPT,
        user_id=_U1,
        attempt_id=503,
        signature=f"emergent.def.{source}",
        confidence=0.5,
        opposes=f"def.{source}",
        evidence_span="span",
        source=source,
    )
    await db.commit()
    assert n == 1


@pytest.mark.asyncio
async def test_record_observations_from_canonical_output_unchanged(db):
    # Regression: record_observation's extraction must not alter the existing
    # canonical write path's behavior (dedup, unkeyed bucket, source default).
    payload = _payload(
        misconceptions=[
            {
                "canonical_key": "misc.sign_error",
                "evidence_span": "flipped the sign",
                "confidence": 0.9,
                "opposes": "eq.newton2",
            }
        ]
    )
    n = await store.record_observations_from_canonical(
        db,
        search_space_id=_SPACE,
        concept_id=_CONCEPT,
        user_id=_U1,
        attempt_id=600,
        canonical_payload=payload,
    )
    await db.commit()
    assert n == 1
    row = (await db.execute(select(MisconceptionObservation))).scalars().one()
    assert row.signature == "misc.sign_error"
    assert row.confidence == pytest.approx(0.9)
    assert row.opposes == "eq.newton2"
    assert row.evidence_span == "flipped the sign"
    assert row.source == "grading_artifact"


@pytest.mark.asyncio
async def test_class_misconceptions_full_gradient_sorted_by_trust(db):
    for i, u in enumerate((_U1, _U2, _U3)):
        await _seed(db, attempt_id=50 + i, user_id=u, key="misc.high", conf=1.0)
    await _seed(db, attempt_id=60, user_id=_U1, key="misc.low", conf=1.0)
    await db.commit()
    rows = await store.load_class_misconceptions(
        db, search_space_id=_SPACE, concept_id=_CONCEPT, now=_NOW
    )
    assert [r.signature for r in rows] == ["misc.high", "misc.low"]
    assert rows[0].band == "promoted"
    assert rows[1].band == "observed"
