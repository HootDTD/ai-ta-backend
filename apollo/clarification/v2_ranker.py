"""VoI (value-of-information) ranker for the clarification-v2 pipeline
(integration spec §4, task T5).

Pure module: no NLI/model calls, no DB, no env reads. Given a pool of
``VoICandidate`` (weak/gray/missing reference nodes extracted from an
``IncrementalSnapshot`` by ``v2_gray_candidates``, task T6 — not this
module), ranks them by ``voi = importance * uncertainty`` so the
clarification loop asks about the reference nodes with the highest expected
composite-score payoff per question.

``importance`` is expressed in the same currency as the live grading
composite (``w_n * Δnode_cov + w_e * Δedge_cov``), using weights read fresh
from ``apollo.grading.composite.load_weights()`` — so VoI tracks whatever
the campaign's composite retuning currently is, without forking constants
here.

``edge_gain`` mirrors ``resolver_v2/edges.py``'s final-credit computation
(A-MAJOR-5 fix) using only snapshot data (no re-run NLI): it maps ALL SIX
``EdgeScore.relation_evidence`` values to a tier (no case silently zero),
allows endpoints-tier *promotion* when the hypothetical target credit and
the other endpoint both clear ``_ENDPOINTS_MIN_CREDIT``, and clamps the
result at the appropriate v1 floor — exactly the credit `edges.py` would
assign, so `edge_gain` is the non-negative rise in that credit.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from apollo.clarification.v2_config import ClarificationV2Params
from apollo.grading.composite import CompositeWeights
from apollo.resolver_v2.config import load_params
from apollo.resolver_v2.incremental_types import IncrementalSnapshot
from apollo.resolver_v2.types import EdgeScore

# Mirrored from apollo/resolver_v2/edges.py (§5.6 ladder) — NOT re-imported
# because edges.py's constants are private (`_R_ENTAIL` etc.); duplicating
# the numeric values here is the smallest way to keep resolver_v2 free of a
# clarification import (the one-way dependency rule, spec §2).
_R_ENTAIL: float = 1.0
_R_COOCCUR: float = 0.7
_R_ENDPOINTS: float = 0.4
_ENDPOINTS_MIN_CREDIT: float = 0.7
_V1_EXPLICIT_FLOOR: float = 1.0
_V1_INFERRED_FLOOR: float = 0.5

# Complete 6-entry relation_evidence -> r map (A-MAJOR-5). Every value
# EdgeScore.relation_evidence can hold is listed explicitly -- there is no
# fallback/default lookup that could silently produce r=0 for a real tier.
_EVIDENCE_TO_R: dict[str, float] = {
    "entail": _R_ENTAIL,
    "cooccur": _R_COOCCUR,
    "endpoints": _R_ENDPOINTS,
    "v1_explicit": _V1_EXPLICIT_FLOOR,
    "v1_inferred": _V1_INFERRED_FLOOR,
    "none": 0.0,
}

# Node types the resolver_v2 equation gate caps credit for (mirrors
# resolver_v2/scoring.py's _EQUATION_NODE_TYPES) -- used here only to give
# these nodes the uncertainty floor (§4.2), not to recompute their credit.
_EQUATION_NODE_TYPES: frozenset[str] = frozenset({"equation"})


@dataclass(frozen=True)
class VoICandidate:
    """One reference node in the VoI ranking pool (design §4.1)."""

    canonical_key: str
    node_type: str
    node_credit: float  # current running V2 credit c_u (in [0, 1])
    is_gray: bool
    incident_edges: tuple[EdgeScore, ...]  # ref edges touching this node
    best_window_index: int | None  # from NodeScore.best (co-occurrence hint)


@dataclass(frozen=True)
class VoIScore:
    """Ranked output for one candidate (design §4.1)."""

    candidate: VoICandidate
    importance: float  # expected composite gain if the node resolves
    uncertainty: float  # P(a clarification question flips it)
    voi: float  # importance * uncertainty


def edge_gain(
    edge_score: EdgeScore,
    c_u_target: float,
    c_v: float,
    params: ClarificationV2Params,
) -> float:
    """Non-negative rise in ``edge_score.credit`` if endpoint ``u`` resolves
    to ``c_u_target`` while the other endpoint stays at its current running
    credit ``c_v`` (design §4.2, A-MAJOR-5 fix).

    Mirrors ``edges.py``'s final-credit computation using only snapshot
    data (no NLI re-run):

    1. Recover the recorded tier via the complete 6-entry evidence->r map.
    2. Allow ENDPOINTS-tier *promotion*: if ``min(c_u_target, c_v) >=
       _ENDPOINTS_MIN_CREDIT``, the endpoints tier becomes available even
       when the recorded evidence was weaker (or "none") -- this is the
       hub-reward mechanism. ENTAIL/COOCCUR are never recomputed downward;
       ``r = max(recorded_tier, endpoints_tier_if_applicable)``.
    3. ``graded = r * sqrt(c_u_target * c_v)``.
    4. ``new_credit = max(graded, v1_floor(relation_evidence))`` -- v1
       floors are flat, not ``r*sqrt``, per edges.py.
    5. ``gain = max(0.0, new_credit - edge_score.credit)``.
    """
    recorded_r = _EVIDENCE_TO_R.get(edge_score.relation_evidence, 0.0)

    endpoints_applicable = min(c_u_target, c_v) >= _ENDPOINTS_MIN_CREDIT
    r = max(recorded_r, _R_ENDPOINTS if endpoints_applicable else 0.0)

    graded = r * math.sqrt(max(0.0, c_u_target) * max(0.0, c_v))

    v1_floor = 0.0
    if edge_score.relation_evidence == "v1_explicit":
        v1_floor = _V1_EXPLICIT_FLOOR
    elif edge_score.relation_evidence == "v1_inferred":
        v1_floor = _V1_INFERRED_FLOOR

    new_credit = max(graded, v1_floor)
    return max(0.0, new_credit - edge_score.credit)


def _uncertainty(
    candidate: VoICandidate,
    params: ClarificationV2Params,
    t_low: float,
    t_mid: float,
) -> float:
    """P(a clarification question flips this node) -- §4.2 bump function."""
    if candidate.node_type in _EQUATION_NODE_TYPES:
        # Equation-cap nodes are gray unconditionally (resolver_v2/scoring.py)
        # and are the designed "write out the relationship" recovery target;
        # they always get at least the equation floor regardless of where
        # their (capped) credit sits in the band.
        band_uncertainty = _band_uncertainty(candidate.node_credit, params, t_low, t_mid)
        return max(params.p_equation_floor, band_uncertainty)
    return _band_uncertainty(candidate.node_credit, params, t_low, t_mid)


def _band_uncertainty(
    c_u: float,
    params: ClarificationV2Params,
    t_low: float,
    t_mid: float,
) -> float:
    if c_u <= 0.0:
        return params.p_missing
    if c_u >= t_mid:
        return params.p_near_resolved
    span = t_mid - t_low
    if span <= 0.0:
        # Defensive: malformed band -- fall back to the near-resolved value
        # rather than dividing by zero.
        return params.p_near_resolved
    frac = (t_mid - c_u) / span
    frac = min(1.0, max(0.0, frac))
    return params.p_gray_min + frac * (params.p_gray_max - params.p_gray_min)


def _importance(
    candidate: VoICandidate,
    snapshot: IncrementalSnapshot,
    weights: CompositeWeights,
    params: ClarificationV2Params,
) -> float:
    """importance(u) = w_n * Δnode_cov(u) + w_e * Δedge_cov(u) (§4.2).

    ``|winning_path|`` and ``|all_ref_edges|`` are read from the snapshot's
    aggregate metadata. ``IncrementalSnapshot`` (per its frozen contract,
    resolver_v2/incremental_types.py) does not carry the winning path's
    node-key list separately from the union-of-paths ``node_credits`` map
    -- the ranker's signature (``pool, snapshot, weights, params``) has no
    ``reference_graph`` to look the path up in. The smallest spec-faithful
    reading: use the total number of currently-scored reference nodes
    (``len(snapshot.node_credits)``) as the path-length denominator. This
    is a superset of the true winning path, so it only ever makes
    Δnode_cov *smaller* than the true value -- the conservative direction,
    consistent with the rest of this feature's monotone-lower-bound stance.
    """
    winning_path_len = max(1, len(snapshot.node_credits))
    all_ref_edges = max(1, len(snapshot.edge_scores))

    c_u = candidate.node_credit
    c_u_target = params.voi_target_credit
    delta_node_cov = (c_u_target - c_u) / winning_path_len

    edge_gain_sum = sum(
        edge_gain(
            edge,
            c_u_target,
            _other_endpoint_credit(edge, candidate.canonical_key, snapshot),
            params,
        )
        for edge in candidate.incident_edges
    )
    delta_edge_cov = edge_gain_sum / all_ref_edges

    return weights.w_n * delta_node_cov + weights.w_e * delta_edge_cov


def _other_endpoint_credit(
    edge: EdgeScore, canonical_key: str, snapshot: IncrementalSnapshot
) -> float:
    """Current running credit of the endpoint of ``edge`` that is NOT
    ``canonical_key`` (the candidate under evaluation). Never re-runs NLI --
    reads straight from the snapshot's running node credits."""
    other_key = edge.to_key if edge.from_key == canonical_key else edge.from_key
    return snapshot.node_credits.get(other_key, 0.0)


def rank_by_voi(
    pool: Sequence[VoICandidate],
    snapshot: IncrementalSnapshot,
    weights: CompositeWeights,
    params: ClarificationV2Params,
) -> list[VoIScore]:
    """Rank ``pool`` by ``voi = importance * uncertainty`` (design §4.2).

    ``weights`` should come from a live ``apollo.grading.composite
    .load_weights()`` call at the caller (so VoI tracks whatever the
    composite is currently tuned to); this function itself does not read
    the environment.

    The gray-band thresholds (``t_low``/``t_mid``) are read fresh from
    ``apollo.resolver_v2.config.load_params()`` (never hardcoded here or in
    ``v2_config.py`` -- §8.1 "Gray band source").

    Ties broken by ``(voi desc, node_credit asc, canonical_key asc)`` for
    determinism.
    """
    resolver_params = load_params()
    t_low, t_mid = resolver_params.t_low, resolver_params.t_mid

    scored: list[VoIScore] = []
    for candidate in pool:
        importance = _importance(candidate, snapshot, weights, params)
        uncertainty = _uncertainty(candidate, params, t_low, t_mid)
        voi = importance * uncertainty
        scored.append(
            VoIScore(
                candidate=candidate,
                importance=importance,
                uncertainty=uncertainty,
                voi=voi,
            )
        )

    scored.sort(
        key=lambda s: (-s.voi, s.candidate.node_credit, s.candidate.canonical_key)
    )
    return scored
