"""Pure trust math for the emergent misconception store (memo §6 / OQ2).

DB-free and IO-free — every function is a deterministic function of its
arguments, so the trust gradient is unit-testable without a container. The
constants live in ``apollo.emergent.config``.

    trust = min(1, distinct_students / K) * mean_confidence * recency_factor

``recency_factor`` is an exponential half-life decay on the age of the most
recent observation. Bands (``candidate`` / ``observed`` / ``promoted``) are
*labels over thresholds* (``TAU_PROJECT`` / ``TAU_ASSERT``), never a stored
state machine (memo P3: no binary ``approved`` bit).
"""

from __future__ import annotations

from datetime import UTC, datetime

from apollo.emergent.config import (
    K_DISTINCT_STUDENTS,
    RECENCY_HALFLIFE_DAYS,
    TAU_ASSERT,
    TAU_PROJECT,
    UNKEYED_PREFIX,
)

_SECONDS_PER_DAY: float = 86_400.0


def _as_utc(dt: datetime) -> datetime:
    """Coerce a naive datetime to UTC-aware (SQLite drops tzinfo on round-trip;
    Postgres keeps it). Naive values are treated as UTC so the subtraction below
    never mixes aware/naive operands."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def recency_factor(
    last_seen: datetime | None,
    now: datetime,
    *,
    halflife_days: float = RECENCY_HALFLIFE_DAYS,
) -> float:
    """Exponential half-life decay on the age of the most recent observation.

    Returns ``0.5 ** (age_days / halflife_days)`` clamped to ``[0, 1]``. A
    just-seen signature scores ~1.0; one seen a half-life ago scores 0.5. A
    ``None`` ``last_seen`` (no observations) or a non-positive half-life yields
    ``0.0`` / ``1.0`` respectively (defensive — no decay when disabled)."""
    if last_seen is None:
        return 0.0
    if halflife_days <= 0:
        return 1.0
    age_seconds = (_as_utc(now) - _as_utc(last_seen)).total_seconds()
    if age_seconds <= 0:
        return 1.0
    age_days = age_seconds / _SECONDS_PER_DAY
    factor = 0.5 ** (age_days / halflife_days)
    return max(0.0, min(1.0, factor))


def trust_score(
    *,
    distinct_students: int,
    mean_confidence: float | None,
    last_seen: datetime | None,
    now: datetime,
    k: int = K_DISTINCT_STUDENTS,
    halflife_days: float = RECENCY_HALFLIFE_DAYS,
) -> float:
    """Continuous trust in ``[0, 1]`` (memo §6 / OQ2).

    ``min(1, distinct_students / K) * mean_confidence * recency_factor``. A
    ``None`` ``mean_confidence`` (every contributing observation lacked a
    confidence) is treated as ``0.0`` — we do not assert on evidence we cannot
    weight. ``k <= 0`` degrades to a support term of 1.0 (defensive)."""
    conf = 0.0 if mean_confidence is None else max(0.0, min(1.0, mean_confidence))
    if distinct_students <= 0:
        return 0.0
    support = 1.0 if k <= 0 else min(1.0, distinct_students / k)
    rec = recency_factor(last_seen, now, halflife_days=halflife_days)
    return max(0.0, min(1.0, support * conf * rec))


def is_promotable_signature(signature: str, *, unkeyed_prefix: str = UNKEYED_PREFIX) -> bool:
    """False for the free-form ``unkeyed:*`` bucket, which NEVER promotes (memo
    §4 / OQ1: you cannot assert what you cannot key). True for a keyed
    (``canonical_key``) signature."""
    return not signature.startswith(unkeyed_prefix)


def band(
    trust: float,
    *,
    tau_assert: float = TAU_ASSERT,
    tau_project: float = TAU_PROJECT,
) -> str:
    """Label the trust gradient: ``promoted`` (``>= tau_assert``), ``observed``
    (``>= tau_project``), else ``candidate``. Labels over thresholds — not a
    stored state (memo P3)."""
    if trust >= tau_assert:
        return "promoted"
    if trust >= tau_project:
        return "observed"
    return "candidate"


def is_promoted(
    trust: float,
    signature: str,
    *,
    muted: bool = False,
    tau_assert: float = TAU_ASSERT,
) -> bool:
    """True iff the signature may be asserted by the grader: keyed (promotable),
    ``trust >= tau_assert``, and not muted. ``muted`` is always ``False`` in
    increment 1 (no curation surface yet) — the parameter reserves the memo's
    increment-2 semantics without foreclosing them."""
    if muted:
        return False
    if not is_promotable_signature(signature):
        return False
    return trust >= tau_assert


__all__ = [
    "recency_factor",
    "trust_score",
    "is_promotable_signature",
    "band",
    "is_promoted",
]
