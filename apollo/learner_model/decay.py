"""WU-5B1 §3 Step 0 — between-session decay toward the cold-start prior.

PURE: NO DB, NO LLM, NO Neo4j, NO containers, NO network. Mirrors ``belief.py``
(named module constants, ``import math``, no IO). ``COLD_START_PRIOR`` is NOT
imported here — the caller passes the prior in (keeps this module
dependency-free and the convex blend explicit); the closure in
``learner_update.py`` binds ``COLD_START_PRIOR`` from ``belief.py``.

Step 0 (spec L412-418): ``belief <- (1-w)*belief + w*prior``, ``w = 1 - e^(-k*dt)``,
``k=0.05`` (~14-day half-life). The pull target = ``COLD_START_PRIOR`` (every
component >= 0.20 > 0), so the blend REPAIRS zeros and honors the §3 'never 0'
invariant. ``Δt`` is INTEGER days, anchored to the frozen ``done_ts`` (never
``now()``), clamped >= 0.
"""

from __future__ import annotations

import math

# Decay rate per day (spec L413; half-life = ln 2 / k = 13.8629 days). LOCKED.
DECAY_K: float = 0.05


def decay_weight(dt_days: float, k: float = DECAY_K) -> float:
    """The convex-blend weight ``w = 1 - e^(-k * max(0, dt_days))``.

    The ``max(0, .)`` clamp kills negative-Δt sharpening (clock skew /
    out-of-order retries): a negative dt -> 0 -> w=0 -> identity. ``dt_days=0``
    -> w=0 -> identity (no spurious forgetting on a same-day re-attempt). LOCKED.
    """
    return 1.0 - math.exp(-k * max(0.0, dt_days))


def decay_toward_prior(
    belief: tuple[float, float, float],
    prior: tuple[float, float, float],
    dt_days: float,
    k: float = DECAY_K,
) -> tuple[float, float, float]:
    """Convex blend ``(1-w)*belief + w*prior``, component-wise. Returns a NEW
    tuple.

    A convex combination of two sum-1 probability vectors is itself sum-1 (no
    renormalize needed). Because every ``prior`` component >= 0.20 > 0, the
    result is strictly > 0 for ``dt > 0`` (zero-repair). PURE — never mutates
    inputs.
    """
    w = decay_weight(dt_days, k)
    return tuple((1.0 - w) * b + w * p for b, p in zip(belief, prior, strict=True))
