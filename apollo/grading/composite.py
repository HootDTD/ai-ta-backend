"""Campaign-plan Task A2 — the composite headline-score formula (spec §1/Q5).

``composite = w_n * node_coverage + w_e * edge_coverage - p * misconception_penalty``.
Weights are NAMED constants, env-overridable, and recorded VERBATIM in every
grading artifact (``artifact_build.py`` reads ``CompositeWeights`` straight into
the artifact's ``scores.weights`` block) so retuning during the campaign never
silently changes a past attempt's recorded basis.

Pure module: no DB/Neo4j/LLM imports, ``os.environ`` read only at call time (so
``monkeypatch.setenv`` in tests is honored without a process restart).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Env var names (Task A2). Defaults match the spec's initial weights; the
# campaign's tuning phase overrides them per subject/run, never the code.
_ENV_W_NODE = "APOLLO_COMPOSITE_W_NODE"
_ENV_W_EDGE = "APOLLO_COMPOSITE_W_EDGE"
_ENV_P_MISC = "APOLLO_COMPOSITE_P_MISC"

_DEFAULT_W_NODE = 0.6
_DEFAULT_W_EDGE = 0.25
_DEFAULT_P_MISC = 0.15

# The misconception-penalty confidence floor (Task A2 Step 4): only asserted
# misconceptions resolved at >= this confidence count toward the penalty
# numerator. Named so artifact_build.py never hardcodes the literal.
MISC_CONFIDENCE_FLOOR: float = 0.5

# Composite scores are rounded to this many decimal places so two calls with
# floating-point-equal inputs compare equal in tests/persistence.
_ROUND_DP = 6


@dataclass(frozen=True)
class CompositeWeights:
    """The three named composite-formula constants. Recorded verbatim in every
    artifact's ``scores.weights`` block (spec §1)."""

    w_n: float
    w_e: float
    p: float


def _env_float(name: str, default: float) -> float:
    """Read ``name`` from the environment as a float; fall back to ``default``
    on missing or malformed (mirrors the repo's other env-float readers, e.g.
    ``nli_config``'s int/float parsing conventions)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def load_weights() -> CompositeWeights:
    """Build :class:`CompositeWeights` from the environment, falling back to the
    spec's defaults (``w_n=0.6``, ``w_e=0.25``, ``p=0.15``) on missing/malformed
    values. Read fresh on every call — no process-lived caching — so campaign
    tuning runs can flip env vars between attempts."""
    return CompositeWeights(
        w_n=_env_float(_ENV_W_NODE, _DEFAULT_W_NODE),
        w_e=_env_float(_ENV_W_EDGE, _DEFAULT_W_EDGE),
        p=_env_float(_ENV_P_MISC, _DEFAULT_P_MISC),
    )


def composite_score(
    node_coverage: float,
    edge_coverage: float,
    misconception_penalty: float,
    weights: CompositeWeights,
) -> float:
    """The spec §1/Q5 explicit weighted composite, clamped to ``[0, 1]``.

    ``composite = w_n*node_coverage + w_e*edge_coverage - p*misconception_penalty``.
    Pure arithmetic — no lookups, no IO. Rounded to :data:`_ROUND_DP` decimal
    places so downstream equality checks (tests, persisted JSONB) are stable."""
    raw = (
        weights.w_n * node_coverage
        + weights.w_e * edge_coverage
        - weights.p * misconception_penalty
    )
    clamped = max(0.0, min(1.0, raw))
    return round(clamped, _ROUND_DP)
