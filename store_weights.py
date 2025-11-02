from __future__ import annotations

"""Shared helpers for retrieval store weight overrides.

The retriever uses environment variables with the ``RETRIEVAL_STORE_WEIGHT_*``
prefix to add a constant bias per knowledge store kind. This module centralizes
the default values, parsing, and sanitization rules so both the API and
frontend-facing storage can rely on the same behavior.
"""

import os
from typing import Any, Dict, Iterable, Mapping


WEIGHT_KINDS: tuple[str, ...] = ("textbook", "slides", "notes", "homework", "exams", "other")
WEIGHT_ENV_PREFIX = "RETRIEVAL_STORE_WEIGHT_"

# Default values used when no environment override is supplied.
WEIGHT_DEFAULTS: Dict[str, float] = {
    "textbook": 0.12,
    "slides": 0.06,
    "notes": 0.06,
    "homework": 0.05,
    "exams": 0.05,
    "other": 0.03,
}

# Chosen bounds for UI sliders and backend validation. See final notes for rationale.
WEIGHT_MIN = 0.0
WEIGHT_MAX = 1.0


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def get_env_weight(kind: str) -> float:
    key = kind.strip().lower()
    default = WEIGHT_DEFAULTS.get(key, 0.0)
    env_var = f"{WEIGHT_ENV_PREFIX}{key.upper()}"
    return _float_env(env_var, default)


def get_env_weights(kinds: Iterable[str] | None = None) -> Dict[str, float]:
    keys = tuple(kinds) if kinds is not None else WEIGHT_KINDS
    return {k: get_env_weight(k) for k in keys}


def clamp_weight(value: float, *, minimum: float = WEIGHT_MIN, maximum: float = WEIGHT_MAX) -> float:
    if value < minimum:
        return minimum
    if value > maximum:
        return maximum
    return value


def normalize_weight(value: Any, *, clamp: bool = True, default: float = WEIGHT_MIN) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        num = default
    return clamp_weight(num) if clamp else num


def normalize_weights(
    raw: Mapping[str, Any],
    *,
    clamp: bool = True,
    base: Mapping[str, float] | None = None,
) -> Dict[str, float]:
    """Normalize an incoming mapping of weights.

    Unknown keys are ignored, allowed keys fall back to ``base`` (or env).
    """
    baseline = dict(base or get_env_weights())
    out = baseline.copy()
    for key, value in raw.items():
        norm_key = key.strip().lower()
        if norm_key not in WEIGHT_KINDS:
            continue
        out[norm_key] = normalize_weight(value, clamp=clamp, default=baseline.get(norm_key, WEIGHT_MIN))
    return out

