"""Emergent misconception store — write + derived-on-read seam (memo increment 1).

Write path: ``record_observations_from_canonical`` derives one observation row
per ``(attempt, signature)`` from a ``role='canonical'`` grading-artifact payload
and appends them idempotently (``ON CONFLICT (attempt_id, signature) DO
NOTHING``). Only the canonical role feeds the store (OQ4) — the caller must never
pass a ``role='pair'`` payload.

Read path: ``aggregate_signatures`` rolls the ledger up per signature for one
class+concept IN PYTHON (portable across the SQLite unit harness and real
Postgres; per-class volumes are modest in increment 1 — materialization is
increment 2). ``load_class_misconceptions`` adds the derived continuous
``trust_score`` + ``band``; ``load_promoted_misconceptions_dict`` returns the
keyed, promoted subset in the exact dict shape the grader's
``candidates_from_misconceptions`` reads — so a promoted emergent misconception
becomes a ``misc.*`` candidate exactly like a hand-authored bank entry.

Every function here is a no-op / empty unless the caller has already checked the
``APOLLO_EMERGENT_MISCONCEPTIONS`` flag (the flag gate lives at the two wiring
seams — ``artifact_writer`` and ``candidate_assembly`` — so this module stays a
pure store).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apollo.emergent.config import UNKEYED_PREFIX
from apollo.emergent.trust import band, is_promoted, trust_score
from apollo.persistence.models import MisconceptionObservation

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ObservationRow:
    """A pure pre-DB observation value object (no ``id`` / ``created_at`` — the
    DB owns those). Immutable."""

    signature: str
    confidence: float | None
    opposes: str | None
    evidence_span: str | None


def _signature_for(canonical_key: str | None, concept_id: int | None) -> str:
    """The accumulation-unit signature (memo §4). A non-empty ``canonical_key``
    IS the signature (the strong, promotable case). A free-form misconception
    (no key) lands in the per-concept ``unkeyed:<concept_id>`` bucket, which
    accumulates but never promotes (OQ1)."""
    if canonical_key:
        return canonical_key
    return f"{UNKEYED_PREFIX}{concept_id if concept_id is not None else 'none'}"


def _derive_observation_rows(
    canonical_payload: dict, *, concept_id: int | None
) -> list[_ObservationRow]:
    """Derive the deduped observation rows from a canonical artifact payload.

    Reads the ``misconceptions[]`` block (``{canonical_key, evidence_span,
    confidence, opposes}`` — the richer source, carries ``opposes``) then folds
    in ``node_ledger[]`` rows with ``status='misconception'`` (equivalent
    evidence, memo §4), deduping on signature so an attempt contributes each
    signature at most once. Order-stable (misconceptions[] first)."""
    by_signature: dict[str, _ObservationRow] = {}

    for entry in canonical_payload.get("misconceptions") or []:
        key = entry.get("canonical_key") if "canonical_key" in entry else entry.get("key")
        signature = _signature_for(key, concept_id)
        if signature in by_signature:
            continue
        by_signature[signature] = _ObservationRow(
            signature=signature,
            confidence=entry.get("confidence"),
            opposes=entry.get("opposes"),
            evidence_span=(entry.get("evidence_span") or None),
        )

    for row in canonical_payload.get("node_ledger") or []:
        if row.get("status") != "misconception":
            continue
        signature = _signature_for(row.get("canonical_key"), concept_id)
        if signature in by_signature:
            continue
        by_signature[signature] = _ObservationRow(
            signature=signature,
            confidence=row.get("confidence"),
            opposes=None,  # node_ledger carries no opposes link
            evidence_span=(row.get("evidence_span") or None),
        )

    return list(by_signature.values())


async def record_observations_from_canonical(
    db: AsyncSession,
    *,
    search_space_id: int,
    concept_id: int | None,
    user_id: str,
    attempt_id: int,
    canonical_payload: dict,
) -> int:
    """Append the ledger observations for one canonical Done-grade. Returns the
    number of rows INSERTED (post-dedup, post-conflict). Idempotent: a re-grade
    of the same attempt inserts 0 rows (``ON CONFLICT (attempt_id, signature)``).
    Does NOT commit — the caller owns the transaction boundary."""
    rows = _derive_observation_rows(canonical_payload, concept_id=concept_id)
    if not rows:
        return 0

    dialect = db.bind.dialect.name if db.bind is not None else "postgresql"
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        insert_fn: Any = sqlite_insert
    else:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        insert_fn = pg_insert

    values = [
        {
            "search_space_id": search_space_id,
            "concept_id": concept_id,
            "signature": r.signature,
            "user_id": user_id,
            "attempt_id": attempt_id,
            "confidence": r.confidence,
            "opposes": r.opposes,
            "evidence_span": r.evidence_span,
            "source": "grading_artifact",
        }
        for r in rows
    ]
    stmt = (
        insert_fn(MisconceptionObservation)
        .values(values)
        .on_conflict_do_nothing(
            index_elements=[
                MisconceptionObservation.attempt_id,
                MisconceptionObservation.signature,
            ]
        )
    )
    result = await db.execute(stmt)
    # rowcount is the number actually inserted (conflicts skipped). Fall back to
    # len(values) if the driver does not report it (defensive, non-load-bearing).
    rowcount = getattr(result, "rowcount", None)
    return rowcount if isinstance(rowcount, int) and rowcount >= 0 else len(values)


@dataclass(frozen=True)
class SignatureAggregate:
    """One signature rolled up over the ledger for a class+concept (derived on
    read; nothing is stored)."""

    signature: str
    observation_count: int
    distinct_students: int
    mean_confidence: float | None
    first_seen: datetime | None
    last_seen: datetime | None
    opposes: str | None
    evidence_spans: tuple[str, ...]


@dataclass(frozen=True)
class ClassMisconception:
    """A signature aggregate with its derived trust + band label."""

    signature: str
    observation_count: int
    distinct_students: int
    mean_confidence: float | None
    last_seen: datetime | None
    opposes: str | None
    evidence_spans: tuple[str, ...]
    trust: float
    band: str


async def aggregate_signatures(
    db: AsyncSession,
    *,
    search_space_id: int,
    concept_id: int | None,
) -> list[SignatureAggregate]:
    """Roll the ledger up per signature for one class+concept. Pure derived read
    — GROUP BY done in Python for portability. Returns ``[]`` when
    ``concept_id`` is ``None`` (a NULL concept never keys a promotable bank,
    mirroring the hand-authored bank's applicability rule)."""
    if concept_id is None:
        return []

    result = await db.execute(
        select(MisconceptionObservation).where(
            MisconceptionObservation.search_space_id == search_space_id,
            MisconceptionObservation.concept_id == concept_id,
        )
    )
    obs = result.scalars().all()

    buckets: dict[str, list[MisconceptionObservation]] = {}
    for o in obs:
        buckets.setdefault(cast(str, o.signature), []).append(o)

    aggregates: list[SignatureAggregate] = []
    for signature, rows in buckets.items():
        students = {str(r.user_id) for r in rows}
        confidences = [cast(float, r.confidence) for r in rows if r.confidence is not None]
        mean_conf = (sum(confidences) / len(confidences)) if confidences else None
        seen = [cast(datetime, r.created_at) for r in rows if r.created_at is not None]
        opposes = next((cast(str, r.opposes) for r in rows if r.opposes), None)
        spans = tuple(dict.fromkeys(cast(str, r.evidence_span) for r in rows if r.evidence_span))
        aggregates.append(
            SignatureAggregate(
                signature=signature,
                observation_count=len(rows),
                distinct_students=len(students),
                mean_confidence=mean_conf,
                first_seen=min(seen) if seen else None,
                last_seen=max(seen) if seen else None,
                opposes=opposes,
                evidence_spans=spans,
            )
        )
    # Deterministic order for callers/tests.
    aggregates.sort(key=lambda a: a.signature)
    return aggregates


def _now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(UTC)


async def load_class_misconceptions(
    db: AsyncSession,
    *,
    search_space_id: int,
    concept_id: int | None,
    now: datetime | None = None,
) -> list[ClassMisconception]:
    """Every signature for a class+concept with its derived trust + band (the
    full gradient — the teacher/projection view). Ordered by trust desc."""
    at = _now(now)
    aggregates = await aggregate_signatures(
        db, search_space_id=search_space_id, concept_id=concept_id
    )
    out: list[ClassMisconception] = []
    for agg in aggregates:
        trust = trust_score(
            distinct_students=agg.distinct_students,
            mean_confidence=agg.mean_confidence,
            last_seen=agg.last_seen,
            now=at,
        )
        out.append(
            ClassMisconception(
                signature=agg.signature,
                observation_count=agg.observation_count,
                distinct_students=agg.distinct_students,
                mean_confidence=agg.mean_confidence,
                last_seen=agg.last_seen,
                opposes=agg.opposes,
                evidence_spans=agg.evidence_spans,
                trust=trust,
                band=band(trust),
            )
        )
    out.sort(key=lambda c: c.trust, reverse=True)
    return out


async def load_promoted_misconceptions_dict(
    db: AsyncSession,
    *,
    search_space_id: int,
    concept_id: int | None,
    now: datetime | None = None,
) -> dict:
    """The promoted (``trust >= TAU_ASSERT``, keyed, not muted) emergent
    misconceptions in the exact dict shape ``candidates_from_misconceptions``
    reads: ``{"misconceptions": [{key, trigger_phrases, opposes,
    display_name}]}``. ``trigger_phrases`` are the distinct observed evidence
    spans (the resolver's alias/fuzzy surfaces). Empty when the flag caller
    passes a cold-start (no promoted signatures) — cold-start asserts nothing."""
    promoted = [
        c
        for c in await load_class_misconceptions(
            db, search_space_id=search_space_id, concept_id=concept_id, now=now
        )
        if is_promoted(c.trust, c.signature)
    ]
    return {
        "misconceptions": [
            {
                "key": c.signature,
                "trigger_phrases": list(c.evidence_spans),
                "opposes": c.opposes,
                "display_name": c.signature,
            }
            for c in promoted
        ]
    }


__all__ = [
    "SignatureAggregate",
    "ClassMisconception",
    "record_observations_from_canonical",
    "aggregate_signatures",
    "load_class_misconceptions",
    "load_promoted_misconceptions_dict",
]
