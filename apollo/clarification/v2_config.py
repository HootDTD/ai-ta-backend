"""Clarification V2-ranker flag + parameters (integration spec §8.1, task T1).

Master switch is ``APOLLO_CLARIFICATION_V2_RANKER`` — DEFAULT-OFF. Mirrors
``apollo.resolver_v2.config`` in shape (flag reader + frozen params
dataclass + ``load_*_params`` env overlay): every knob is read FRESH per
call from the environment (no process-lived caching) so tests'
``monkeypatch.setenv`` and calibration sweeps are honored without a
restart. Malformed env values fall back to the field default.

The gray band (``t_low``/``t_mid``/``t_high``) is deliberately NOT defined
here — the ranker consumes it verbatim from
``apollo.resolver_v2.config.load_params()`` so trigger tracks grading
calibration automatically (spec §8.1 "Gray band source"). This module must
never fork or shadow those constants.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

CLARIFICATION_V2_RANKER_FLAG: str = "APOLLO_CLARIFICATION_V2_RANKER"

_TRUTHY: tuple[str, ...] = ("1", "true", "yes")


def _env_truthy(name: str) -> bool:
    """DEFAULT-OFF flag reader: unset -> False; truthy = ``1|true|yes``
    (case-insensitive, stripped). Malformed/anything else -> False."""
    raw = os.environ.get(name)
    if raw is None:
        return False
    return raw.strip().lower() in _TRUTHY


def clarification_v2_ranker_enabled() -> bool:
    """Whether the V2 VoI ranker drives clarification question selection
    (integration spec §1/§8). DEFAULT-OFF, read fresh per call (no
    caching): unset or malformed means the byte-identical v1 selection
    path runs."""
    return _env_truthy(CLARIFICATION_V2_RANKER_FLAG)


def _env_float(name: str, default: float) -> float:
    """Read ``name`` from the environment as a float; fall back to
    ``default`` on missing or malformed (mirrors resolver_v2/config.py)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    """Read ``name`` from the environment as an int; fall back to
    ``default`` on missing or malformed."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class ClarificationV2Params:
    """Every V2-ranker tunable (integration spec §8.1). The gray band
    (t_low/t_mid/t_high) is intentionally absent — consume
    ``resolver_v2.config.load_params()`` verbatim for that."""

    # §4.3 packing limits
    max_questions: int = 3
    max_topics_per_question: int = 3
    # §10 cumulative per-attempt cap (M4)
    max_questions_per_attempt: int = 12
    # §4.2 importance: optimistic full-resolution ceiling for ranking
    voi_target_credit: float = 1.0
    # §4.2 uncertainty band constants
    p_missing: float = 0.6
    p_near_resolved: float = 0.2
    p_gray_min: float = 0.3
    p_gray_max: float = 0.8
    p_equation_floor: float = 0.7
    # §5.5 background incremental job budget
    incremental_deadline_ms: int = 1500
    # §7 trace/observability
    trace_top_n: int = 10


# Single source of the construction defaults for load_clarification_v2_params,
# so the values above are the only place defaults are declared.
_DEFAULTS: ClarificationV2Params = ClarificationV2Params()


def load_clarification_v2_params() -> ClarificationV2Params:
    """Build :class:`ClarificationV2Params` from the environment: class
    defaults with any ``APOLLO_CLARIFICATION_V2_<FIELD>`` override applied
    on top. Missing/malformed -> default."""
    return ClarificationV2Params(
        max_questions=_env_int(
            "APOLLO_CLARIFICATION_V2_MAX_QUESTIONS", _DEFAULTS.max_questions
        ),
        max_topics_per_question=_env_int(
            "APOLLO_CLARIFICATION_V2_MAX_TOPICS_PER_QUESTION",
            _DEFAULTS.max_topics_per_question,
        ),
        max_questions_per_attempt=_env_int(
            "APOLLO_CLARIFICATION_V2_MAX_QUESTIONS_PER_ATTEMPT",
            _DEFAULTS.max_questions_per_attempt,
        ),
        voi_target_credit=_env_float(
            "APOLLO_CLARIFICATION_V2_VOI_TARGET_CREDIT",
            _DEFAULTS.voi_target_credit,
        ),
        p_missing=_env_float(
            "APOLLO_CLARIFICATION_V2_P_MISSING", _DEFAULTS.p_missing
        ),
        p_near_resolved=_env_float(
            "APOLLO_CLARIFICATION_V2_P_NEAR_RESOLVED", _DEFAULTS.p_near_resolved
        ),
        p_gray_min=_env_float(
            "APOLLO_CLARIFICATION_V2_P_GRAY_MIN", _DEFAULTS.p_gray_min
        ),
        p_gray_max=_env_float(
            "APOLLO_CLARIFICATION_V2_P_GRAY_MAX", _DEFAULTS.p_gray_max
        ),
        p_equation_floor=_env_float(
            "APOLLO_CLARIFICATION_V2_P_EQUATION_FLOOR",
            _DEFAULTS.p_equation_floor,
        ),
        incremental_deadline_ms=_env_int(
            "APOLLO_CLARIFICATION_V2_INCREMENTAL_DEADLINE_MS",
            _DEFAULTS.incremental_deadline_ms,
        ),
        trace_top_n=_env_int(
            "APOLLO_CLARIFICATION_V2_TRACE_TOP_N", _DEFAULTS.trace_top_n
        ),
    )
