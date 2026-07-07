"""Resolver V2 graded edge credit (design §5.6, task T5).

Kills the v1 quadratic edge starvation: instead of the binary
both-endpoints-resolved AND parsed-edge gate, every reference edge gets a
*graded* credit from a 3-tier relation-evidence ladder:

    r(e) = max of the applicable tiers
      1.0  ENTAIL     a top-2 prefiltered window entails the verbalized edge
                      (NLI gated by min endpoint credit >= 0.3 OR the
                      endpoints' best windows co-occurring — the budget guard)
      0.7  COOCCUR    both endpoints' argmax windows are the same or adjacent
                      (the ALA-Reader/GIKS co-occurrence move)
      0.4  ENDPOINTS  both endpoint credits >= 0.7 anywhere in the transcript
      0.0  otherwise

    edge_credit(e) = r(e) * sqrt(c'_u * c'_v)
      c'_x = max(c_x, edge_pullup_floor)  when r(e) came from ENTAIL
           = c_x                          otherwise

An ENTAIL-evidenced edge also *pulls up* both endpoint node credits to
``edge_pullup_floor`` (the R3 "joint" move, ILP-free) — returned as a floors
dict the engine (T7) applies to node credits BEFORE path aggregation.

v1 floor (max wins): an edge v1 already matched keeps its v1 credit —
explicit triple 1.0, inferred-only 0.5 (v1 ``scores.py`` semantics) — so V2
edge coverage is >= v1's by construction.

Pure + deterministic: no network, no model load, no DB. The NLI adjudicator
and the window selector arrive as arguments (tests inject fakes).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from apollo.graph_compare.canonical import CanonicalEdge
from apollo.resolution.nli_adjudicator import NLIAdjudicator
from apollo.resolver_v2.config import ResolverV2Params
from apollo.resolver_v2.types import EdgeScore, NodeScore, SelectFn, Window

# §5.6 relation-evidence ladder values (design-fixed, not calibration surface).
_R_ENTAIL: float = 1.0
_R_COOCCUR: float = 0.7
_R_ENDPOINTS: float = 0.4

#: ENDPOINTS tier: both endpoint credits must reach this anywhere (§5.6).
_ENDPOINTS_MIN_CREDIT: float = 0.7
#: COOCCUR tier: |best_window(u) - best_window(v)| <= this (same or adjacent).
_COOCCUR_MAX_DISTANCE: int = 1
#: ENTAIL NLI budget guard: run NLI only when min endpoint credit reaches
#: this OR the endpoints' best windows co-occur (§5.6).
_ENTAIL_GATE_MIN_CREDIT: float = 0.3
#: Windows per verbalized edge sent to NLI ("max over top-2 windows", §5.6).
_EDGE_NLI_TOP_K: int = 2

# v1 floor values — mirror v1 scores.py edge semantics (§5.6).
_V1_EXPLICIT_FLOOR: float = 1.0
_V1_INFERRED_FLOOR: float = 0.5


def verbalize_edge(edge_type: str, from_label: str, to_label: str) -> str:
    """Deterministic natural-language hypothesis for a reference edge
    (§5.6 templates; direction conventions match ``build_reference_canonical``:
    USES proc→eq, DEPENDS_ON dep→step, PRECEDES prev→next, SCOPES cond→target).

    Unknown edge types get a defensive generic template — never raises (the
    ontology guarantees the four known types; this guards drift)."""
    if edge_type == "USES":
        return f"{from_label} uses {to_label}."
    if edge_type == "DEPENDS_ON":
        return f"{to_label} requires {from_label}."
    if edge_type == "PRECEDES":
        return f"{from_label} happens before {to_label}."
    if edge_type == "SCOPES":
        return f"{to_label} applies when {from_label}."
    return f"{from_label} relates to {to_label}."


def _credit_of(node_scores: Mapping[str, NodeScore], key: str) -> float:
    """Endpoint credit; a key absent from ``node_scores`` scores 0.0
    (defensive: every union-of-paths key has a NodeScore, but a reference
    edge may touch an off-path node)."""
    node = node_scores.get(key)
    return node.credit if node is not None else 0.0


def _best_windows_cooccur(
    node_scores: Mapping[str, NodeScore], u: str, v: str
) -> bool:
    """COOCCUR test: both endpoints have an argmax pair and their best
    windows are the same or adjacent (distance <= 1)."""
    u_node = node_scores.get(u)
    v_node = node_scores.get(v)
    if u_node is None or v_node is None:
        return False
    if u_node.best is None or v_node.best is None:
        return False
    distance = abs(u_node.best.window_index - v_node.best.window_index)
    return distance <= _COOCCUR_MAX_DISTANCE


def _edge_entailed(
    hypothesis: str,
    windows: Sequence[Window],
    window_by_index: Mapping[int, Window],
    *,
    nli: NLIAdjudicator,
    params: ResolverV2Params,
    select_fn: SelectFn,
    budget_left: int,
) -> tuple[bool, int]:
    """Run the ENTAIL tier: classify the verbalized edge against its top-2
    prefiltered windows; entailed iff the max P(entailment) reaches
    ``t_edge``. Returns ``(entailed, pairs_used)``. Stops early on the first
    qualifying window (max >= threshold ⟺ any >= threshold) and never
    exceeds ``budget_left``."""
    pairs_used = 0
    for window_index, _lex in select_fn(windows, hypothesis, _EDGE_NLI_TOP_K):
        if pairs_used >= budget_left:
            break
        window = window_by_index.get(window_index)
        if window is None:  # defensive: selector returned an unknown index
            continue
        result = nli.classify(window.text, hypothesis)
        pairs_used += 1
        if result.entailment >= params.t_edge:
            return (True, pairs_used)
    return (False, pairs_used)


def score_edges(
    ref_edges: Sequence[CanonicalEdge],
    node_scores: Mapping[str, NodeScore],
    windows: Sequence[Window],
    *,
    labels: Mapping[str, str],  # canonical_key -> label
    nli: NLIAdjudicator | None,
    params: ResolverV2Params,
    v1_explicit_triples: frozenset[tuple[str, str, str]],  # (type, from, to)
    v1_inferred_triples: frozenset[tuple[str, str, str]],
    select_fn: SelectFn,
    pair_budget_left: int,
) -> tuple[tuple[EdgeScore, ...], dict[str, float], int]:
    """Grade every reference edge per the §5.6 ladder.

    Returns ``(edge_scores in input order, node pull-up floors
    {canonical_key: edge_pullup_floor}, nli_pairs_used)``.

    - ``nli=None`` runs the ladder without the ENTAIL tier (deterministic
      degrade — COOCCUR/ENDPOINTS/v1 floors still apply).
    - Edges are scored independently on the INPUT node credits: a pull-up
      earned by one edge does not feed another edge's gate or formula in the
      same pass (the engine applies the returned floors to node credits
      before aggregation, T7).
    - ``relation_evidence`` records what determined the final credit: the
      winning r-tier, or ``"v1_explicit"``/``"v1_inferred"`` when the v1
      floor strictly exceeds the graded credit (ties keep the graded tier).
    - Never raises on missing endpoints/labels; no NaN (credits and r are
      finite, sqrt operand >= 0 by construction).
    """
    window_by_index: dict[int, Window] = {w.index: w for w in windows}
    budget = max(0, pair_budget_left)
    pairs_used = 0
    pullups: dict[str, float] = {}
    scored: list[EdgeScore] = []

    for edge in ref_edges:
        edge_type = str(edge.edge_type)  # EdgeType is a StrEnum -> plain value
        u, v = edge.from_key, edge.to_key
        c_u = _credit_of(node_scores, u)
        c_v = _credit_of(node_scores, v)
        cooccur = _best_windows_cooccur(node_scores, u, v)

        # ENTAIL tier — NLI gated (budget guard, §5.6).
        entailed = False
        gate_open = min(c_u, c_v) >= _ENTAIL_GATE_MIN_CREDIT or cooccur
        if nli is not None and gate_open and pairs_used < budget:
            hypothesis = verbalize_edge(
                edge_type, labels.get(u, u), labels.get(v, v)
            )
            entailed, used = _edge_entailed(
                hypothesis,
                windows,
                window_by_index,
                nli=nli,
                params=params,
                select_fn=select_fn,
                budget_left=budget - pairs_used,
            )
            pairs_used += used

        # r(e) = max of the applicable tiers (ENTAIL > COOCCUR > ENDPOINTS).
        if entailed:
            r, evidence = _R_ENTAIL, "entail"
        elif cooccur:
            r, evidence = _R_COOCCUR, "cooccur"
        elif c_u >= _ENDPOINTS_MIN_CREDIT and c_v >= _ENDPOINTS_MIN_CREDIT:
            r, evidence = _R_ENDPOINTS, "endpoints"
        else:
            r, evidence = 0.0, "none"

        # Endpoint credits for the formula + the node pull-up (ENTAIL only).
        if entailed:
            c_u_eff = max(c_u, params.edge_pullup_floor)
            c_v_eff = max(c_v, params.edge_pullup_floor)
            pullups[u] = max(pullups.get(u, 0.0), params.edge_pullup_floor)
            pullups[v] = max(pullups.get(v, 0.0), params.edge_pullup_floor)
        else:
            c_u_eff, c_v_eff = c_u, c_v

        credit = r * math.sqrt(c_u_eff * c_v_eff)

        # v1 floor — max wins; a strictly-higher floor takes the evidence tag.
        triple = (edge_type, u, v)
        if triple in v1_explicit_triples and _V1_EXPLICIT_FLOOR > credit:
            credit, evidence = _V1_EXPLICIT_FLOOR, "v1_explicit"
        elif triple in v1_inferred_triples and _V1_INFERRED_FLOOR > credit:
            credit, evidence = _V1_INFERRED_FLOOR, "v1_inferred"

        scored.append(
            EdgeScore(
                edge_type=edge_type,
                from_key=u,
                to_key=v,
                credit=credit,
                relation_evidence=evidence,
            )
        )

    return (tuple(scored), pullups, pairs_used)
