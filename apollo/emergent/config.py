"""Flag + trust-gradient constants for the emergent misconception store.

The flag ``APOLLO_EMERGENT_MISCONCEPTIONS`` defaults **OFF**. While OFF the
write path never fires and the read path returns nothing (see
``apollo.emergent.store``), so grading output is byte-identical to the
no-store behavior.

The trust-gradient constants are the design memo's OQ2 DEFAULTS (2026-07-05
§7.2). They are **pre-calibration** — a human must approve or replace them
before the flag is enabled live (see the PR's "What a human must decide"). Each
is env-overridable so calibration can retune without a code change.

    trust = min(1, distinct_students / K) * mean_confidence * recency_factor

  * ``K_DISTINCT_STUDENTS`` (K=3): distinct students at which the class-support
    term saturates at 1.0.
  * ``TAU_ASSERT`` (0.5): a signature with ``trust >= TAU_ASSERT`` (and not
    muted, and keyed) is **promoted** — the grader may assert it as a
    ``misc.*`` candidate.
  * ``TAU_PROJECT`` (0.2): the lower band a teacher-facing projection surfaces
    (the gradient is visible to teachers before the grader acts on it).
  * ``RECENCY_HALFLIFE_DAYS`` (30): the memo mandates a recency half-life but
    left the value open; 30 days is the pre-calibration default. Trust decays by
    one half per half-life since the signature was last observed.
"""

from __future__ import annotations

import os

# Feature flag — default OFF. Accepts the repo's standard truthy set (mirrors
# apollo/provision_worker.py, apollo/learner_janitor_worker.py).
_FLAG_ENV: str = "APOLLO_EMERGENT_MISCONCEPTIONS"
_TRUTHY: frozenset[str] = frozenset({"1", "true", "yes", "on"})


def emergent_misconceptions_enabled() -> bool:
    """True iff ``APOLLO_EMERGENT_MISCONCEPTIONS`` is set to a truthy value.

    Read at call time (never cached) so a test can flip it per-case and a
    deploy can flip it without a restart, matching the other Apollo flags."""
    return os.environ.get(_FLAG_ENV, "").strip().lower() in _TRUTHY


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# OQ2 defaults (pre-calibration; env-overridable). Read at import time — these
# are tuning constants, not per-request state, so import-time binding is fine
# (calibration sets the env before the process starts).
K_DISTINCT_STUDENTS: int = _int_env("APOLLO_EMERGENT_K", 3)
TAU_ASSERT: float = _float_env("APOLLO_EMERGENT_TAU_ASSERT", 0.5)
TAU_PROJECT: float = _float_env("APOLLO_EMERGENT_TAU_PROJECT", 0.2)
RECENCY_HALFLIFE_DAYS: float = _float_env("APOLLO_EMERGENT_RECENCY_HALFLIFE_DAYS", 30.0)

# Free-form (opposes=None, no canonical_key) observations accumulate under a
# per-concept bucket keyed with this prefix; they NEVER promote (memo §4 / OQ1).
UNKEYED_PREFIX: str = "unkeyed:"

__all__ = [
    "emergent_misconceptions_enabled",
    "K_DISTINCT_STUDENTS",
    "TAU_ASSERT",
    "TAU_PROJECT",
    "RECENCY_HALFLIFE_DAYS",
    "UNKEYED_PREFIX",
]
