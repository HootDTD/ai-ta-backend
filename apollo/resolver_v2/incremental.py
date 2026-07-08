"""Resolver V2 incremental per-turn scorer (design §5.3, task T3).

``score_turn`` is the hot-chat-turn accelerator for the clarification-v2
ranker (§3.1): it COMPOSES the same pure functions the batch engine
(``engine.run_resolver_v2``) uses -- ``build_windows``, ``score_nodes``,
``apply_grayzone``, ``select_windows``, ``score_edges``, ``aggregate`` -- but
only over the *new* student turns each call, carrying a small running state
(``IncrementalState``) forward instead of re-scoring the whole transcript.

It is NOT the grading source of truth. Per §5.4 it is a **conservative
monotone lower bound** on the from-scratch batch grade: exact equality holds
only on the pure-ladder monotone fixture (no gray-zone upgrade, no v1 edge
floors, no budget truncation); every other divergence (A-MAJOR-1/3/4, budget
truncation) is forced into the under-crediting direction so a probed node can
only ever be more likely to get asked about, never wrongly dropped from the
pool.

Pure/deterministic given its injected callables (``nli``, ``grayzone_fn``,
``select_fn``): no DB, no Neo4j, no env reads. Every update returns a NEW
``IncrementalState``/``IncrementalSnapshot`` -- never mutates the input state.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import replace

from apollo.graph_compare.canonical import ReferenceGraph
from apollo.resolution.nli_adjudicator import NLIAdjudicator
from apollo.resolver_v2.aggregate import aggregate
from apollo.resolver_v2.config import ResolverV2Params
from apollo.resolver_v2.edges import score_edges
from apollo.resolver_v2.grayzone import GrayzoneFn, apply_grayzone
from apollo.resolver_v2.incremental_types import IncrementalSnapshot, IncrementalState
from apollo.resolver_v2.scoring import credit_for_score, score_nodes
from apollo.resolver_v2.types import EdgeScore, NodeScore, RefNode, SelectFn, Window
from apollo.resolver_v2.windows import build_windows

#: Ontology node types capped by the symbolic-blindness gate (mirrors
#: ``scoring._EQUATION_NODE_TYPES`` -- kept in lockstep by hand since that
#: name is private to ``scoring.py``; "equation" is the only equation type in
#: ``apollo.ontology.nodes``).
_EQUATION_NODE_TYPES: frozenset[str] = frozenset({"equation"})

#: Sources ``score_nodes``/this module may assign that still qualify for the
#: §6 gray-zone check (mirrors ``engine._GRAY_UPGRADABLE_SOURCES``).
_GRAY_UPGRADABLE_SOURCES: frozenset[str] = frozenset({"nli", "lexical_skip", "equation_cap"})

# edges.py's ladder constants, mirrored here for the RUNNING recompute step
# (§5.3 step 5): every turn recomputes every reference edge's credit from the
# running (merged-across-turns) node credits x the running relation-evidence
# tier, rather than re-deriving it from a fresh ``score_edges`` call over the
# whole transcript.
_R_ENTAIL: float = 1.0
_R_COOCCUR: float = 0.7
_R_ENDPOINTS: float = 0.4
_R_NONE: float = 0.0
_EDGE_R_BY_EVIDENCE: dict[str, float] = {
    "entail": _R_ENTAIL,
    "cooccur": _R_COOCCUR,
    "endpoints": _R_ENDPOINTS,
    "none": _R_NONE,
}
# v1 edge floors mirror v1 ``scores.py`` semantics (edges.py's
# ``_V1_EXPLICIT_FLOOR``/``_V1_INFERRED_FLOOR``) -- a flat override, not run
# through the r*sqrt(c_u*c_v) formula.
_V1_EXPLICIT_FLOOR: float = 1.0
_V1_INFERRED_FLOOR: float = 0.5

#: A-MAJOR-1 merge-priority ceiling per evidence tier used ONLY to decide
#: which of (running, this-turn) evidence wins the max-merge into
#: ``running_edge_evidence``: entail ties with v1_explicit (both cap credit
#: at 1.0) -- a tie keeps whichever is already stored (never regresses);
#: cooccur > v1_inferred > endpoints > none, per §5.2's stated order
#: ("entail > cooccur > endpoints > v1 tiers > none" -- v1_inferred is placed
#: above endpoints here because its guaranteed flat credit, 0.5, already
#: exceeds any endpoints-tier credit, which is capped at
#: 0.4 * sqrt(1.0*1.0) == 0.4).
_EVIDENCE_CEILING: dict[str, float] = {
    "entail": 1.0,
    "v1_explicit": 1.0,
    "cooccur": 0.7,
    "v1_inferred": 0.5,
    "endpoints": 0.4,
    "none": 0.0,
}

_NONE_EVIDENCE = "none"


def _edge_key(edge_type: str, from_key: str, to_key: str) -> str:
    """Deterministic ``running_edge_evidence`` key: ``"TYPE|from|to"``."""
    return f"{edge_type}|{from_key}|{to_key}"


def _cap_equation_credit(
    node_type: str, credit: float, source: str, params: ResolverV2Params
) -> tuple[float, str]:
    """Symbolic-blindness gate (mirrors ``scoring._cap_equation_text_credit``),
    applied here to the RUNNING (merged-across-turns) score rather than a
    single turn's -- equation_cap runs BEFORE the v1 floor, exactly as batch."""
    if node_type not in _EQUATION_NODE_TYPES:
        return credit, source
    if credit <= params.equation_text_credit_cap:
        return credit, source
    return params.equation_text_credit_cap, "equation_cap"


def _offset_windows(
    local_windows: Sequence[Window], *, global_window_count: int, turn_offset: int
) -> tuple[Window, ...]:
    """§5.3 step 1a: rewrite ``build_windows``'s 0-based output into the
    scorer's continuous global index/turn_index space. ``build_windows``
    windows each student turn independently (no window ever spans a turn
    boundary -- see ``windows.py``), so windowing only the NEW suffix turns
    reproduces byte-identical window texts to a full-transcript batch build;
    no cross-turn overlap text needs to be re-derived. Only the index/
    turn_index bookkeeping needs the offset rewrite."""
    return tuple(
        Window(
            index=global_window_count + i,
            turn_index=turn_offset + window.turn_index,
            text=window.text,
        )
        for i, window in enumerate(local_windows)
    )


def _gray_eligible(source: str, score: float, params: ResolverV2Params) -> bool:
    """Mirrors ``engine._gray_nodes``'s per-node predicate: an
    ``equation_cap`` node is gray UNCONDITIONALLY; any other upgradable
    source is gray only when its (raw, pre-ladder) score lands in the
    ``t_low <= score < t_mid`` band."""
    if source not in _GRAY_UPGRADABLE_SOURCES:
        return False
    if source == "equation_cap":
        return True
    _credit, is_gray = credit_for_score(score, params)
    return is_gray


def score_turn(
    state: IncrementalState,
    *,
    all_student_turns: Sequence[str],
    reference_graph: ReferenceGraph,
    problem_payload: dict,
    v1_resolved_keys: frozenset[str],
    nli: NLIAdjudicator | None,
    grayzone_fn: GrayzoneFn | None,
    select_fn: SelectFn,
    params: ResolverV2Params,
    ref_nodes: Sequence[RefNode],
    v1_explicit_triples: frozenset[tuple[str, str, str]] = frozenset(),
    v1_inferred_triples: frozenset[tuple[str, str, str]] = frozenset(),
) -> tuple[IncrementalState, IncrementalSnapshot]:
    """Score the turns appended since ``state.window_cursor`` and return the
    updated ``(IncrementalState, IncrementalSnapshot)`` (§5.3). ``problem_payload``
    is accepted for signature parity with the batch engine's inputs (labels
    are read from ``ref_nodes`` here, which the caller built once) -- it is
    not otherwise used in this pure step.
    """
    del problem_payload  # signature parity only (see docstring)

    # --- step 1 / 1a: new windows, reindexed into the continuous global
    # index/turn_index space. -------------------------------------------------
    new_turns = list(all_student_turns[state.window_cursor :])
    local_windows = build_windows(new_turns, params)
    new_windows = _offset_windows(
        local_windows,
        global_window_count=state.global_window_count,
        turn_offset=state.window_cursor,
    )

    # --- step 2: unresolved-only candidate set (frozen resolved/seeded nodes
    # spend zero pairs). -------------------------------------------------------
    candidate_ref_nodes = tuple(
        node
        for node in ref_nodes
        if state.running_node_max.get(node.canonical_key, 0.0) < params.t_high
        and node.canonical_key not in state.seeded_keys
    )

    # --- step 3: score, budget-guarded. ---------------------------------------
    node_budget_left = max(0, params.max_nli_pairs - state.pair_count_total)
    node_params = replace(params, max_nli_pairs=node_budget_left)
    this_turn_node_scores, node_pairs = score_nodes(
        new_windows,
        candidate_ref_nodes,
        nli=nli,
        params=node_params,
        v1_resolved_keys=v1_resolved_keys,
        select_fn=select_fn,
    )
    this_turn_by_key: dict[str, NodeScore] = {
        ns.canonical_key: ns for ns in this_turn_node_scores
    }

    # --- step 4 / 4a: append-only max merge, then reapply the credit ladder
    # (equation_cap before v1_floor) on the MERGED running score. Seeded keys
    # override to 1.0 last. ----------------------------------------------------
    running_score: dict[str, float] = {}
    running_source: dict[str, str] = {}
    running_credit: dict[str, float] = {}
    running_best = {ns.canonical_key: ns.best for ns in this_turn_node_scores}

    for node in ref_nodes:
        key = node.canonical_key
        old_score = state.running_node_max.get(key, 0.0)
        old_source = state.node_source.get(key, "zero")
        candidate = this_turn_by_key.get(key)
        this_score = candidate.score if candidate is not None else old_score
        this_source = candidate.source if candidate is not None else old_source

        if this_score > old_score:
            merged_score, merged_source = this_score, this_source
        else:
            merged_score, merged_source = old_score, old_source

        credit, _is_gray = credit_for_score(merged_score, params)
        credit, merged_source = _cap_equation_credit(
            node.node_type, credit, merged_source, params
        )
        if key in v1_resolved_keys and credit < 1.0:
            credit, merged_source = 1.0, "v1_floor"
        if key in state.seeded_keys:
            credit, merged_source = 1.0, "seeded"

        running_score[key] = merged_score
        running_source[key] = merged_source
        running_credit[key] = credit

    # --- step 4b: SAME apply_grayzone stage as batch, same injected fn. -------
    gray_candidates = [
        node
        for node in ref_nodes
        if _gray_eligible(running_source[node.canonical_key], running_score[node.canonical_key], params)
    ]
    gray_node_scores = tuple(
        NodeScore(
            canonical_key=node.canonical_key,
            score=running_score[node.canonical_key],
            credit=running_credit[node.canonical_key],
            source=running_source[node.canonical_key],
            best=running_best.get(node.canonical_key),
        )
        for node in gray_candidates
    )
    ref_by_key = {node.canonical_key: node for node in ref_nodes}
    transcript = "\n".join(all_student_turns)
    upgrades = apply_grayzone(gray_node_scores, transcript, grayzone_fn, params, ref_nodes=ref_by_key)
    for key, upgraded_credit in upgrades.items():
        if upgraded_credit > running_credit[key]:
            running_credit[key] = upgraded_credit
            running_source[key] = "grayzone"
    gray_after_upgrade = frozenset(
        node.canonical_key for node in gray_candidates if node.canonical_key not in upgrades
    )

    # --- step 5: edges (monotone, A-MAJOR-1). ---------------------------------
    # ``best`` is only carried into the COOCCUR test when the node's running
    # credit actually cleared the zero/none band this turn: ``score_nodes``
    # always records SOME "best" pair once NLI runs, even a near-zero-fused
    # one, and with few windows available early on two unrelated nodes can
    # coincidentally share their only candidate window -- a false COOCCUR
    # that ``running_edge_evidence``'s monotone merge would then lock in
    # forever. Gating on non-zero credit keeps this conservative (never
    # invents relational evidence out of a near-zero match) without losing
    # any REAL cooccur signal (a genuine match always clears the zero band).
    node_for_edges: dict[str, NodeScore] = {
        node.canonical_key: NodeScore(
            canonical_key=node.canonical_key,
            score=running_score[node.canonical_key],
            credit=running_credit[node.canonical_key],
            source=running_source[node.canonical_key],
            best=(
                running_best.get(node.canonical_key)
                if running_credit[node.canonical_key] > 0.0
                else None
            ),
        )
        for node in ref_nodes
    }
    labels = {node.canonical_key: node.label for node in ref_nodes}
    edge_budget_left = max(0, params.max_nli_pairs - state.pair_count_total - node_pairs)
    this_turn_edge_scores, _this_turn_pullups, edge_pairs = score_edges(
        reference_graph.edges,
        node_for_edges,
        new_windows,
        labels=labels,
        nli=nli,
        params=params,
        v1_explicit_triples=v1_explicit_triples,
        v1_inferred_triples=v1_inferred_triples,
        select_fn=select_fn,
        pair_budget_left=edge_budget_left,
    )
    this_turn_evidence_by_key = {
        _edge_key(es.edge_type, es.from_key, es.to_key): es.relation_evidence
        for es in this_turn_edge_scores
    }

    running_edge_evidence: dict[str, str] = dict(state.running_edge_evidence)
    for edge in reference_graph.edges:
        edge_type = str(edge.edge_type)
        key = _edge_key(edge_type, edge.from_key, edge.to_key)
        old_evidence = running_edge_evidence.get(key, _NONE_EVIDENCE)
        new_evidence = this_turn_evidence_by_key.get(key, _NONE_EVIDENCE)
        old_ceiling = _EVIDENCE_CEILING.get(old_evidence, 0.0)
        new_ceiling = _EVIDENCE_CEILING.get(new_evidence, 0.0)
        # Tie keeps whichever was stored (§5.2: both yield the same ceiling).
        running_edge_evidence[key] = new_evidence if new_ceiling > old_ceiling else old_evidence

    # ENTAIL-evidenced edges pull both endpoints up to edge_pullup_floor --
    # based on the RUNNING (persisted) evidence, not just this turn's, so a
    # pull-up earned in an earlier turn is never lost.
    for edge in reference_graph.edges:
        edge_type = str(edge.edge_type)
        key = _edge_key(edge_type, edge.from_key, edge.to_key)
        if running_edge_evidence.get(key) != "entail":
            continue
        for endpoint in (edge.from_key, edge.to_key):
            if endpoint in running_credit and params.edge_pullup_floor > running_credit[endpoint]:
                running_credit[endpoint] = params.edge_pullup_floor
                running_source[endpoint] = "edge_pullup"

    # Recompute every reference edge's credit from the RUNNING node credits x
    # RUNNING evidence tier (deterministic; equals batch on the
    # entail/cooccur/endpoints/none ladder -- v1 edge floors are the one
    # intentional, conservative divergence, A-MAJOR-4).
    final_edge_scores: list[EdgeScore] = []
    for edge in reference_graph.edges:
        edge_type = str(edge.edge_type)
        u, v = edge.from_key, edge.to_key
        key = _edge_key(edge_type, u, v)
        evidence = running_edge_evidence.get(key, _NONE_EVIDENCE)
        c_u = running_credit.get(u, 0.0)
        c_v = running_credit.get(v, 0.0)
        if evidence == "v1_explicit":
            credit = _V1_EXPLICIT_FLOOR
        elif evidence == "v1_inferred":
            credit = _V1_INFERRED_FLOOR
        else:
            r = _EDGE_R_BY_EVIDENCE.get(evidence, _R_NONE)
            if evidence == "entail":
                c_u_eff = max(c_u, params.edge_pullup_floor)
                c_v_eff = max(c_v, params.edge_pullup_floor)
            else:
                c_u_eff, c_v_eff = c_u, c_v
            credit = r * math.sqrt(c_u_eff * c_v_eff)
        final_edge_scores.append(
            EdgeScore(edge_type=edge_type, from_key=u, to_key=v, credit=credit, relation_evidence=evidence)
        )

    # --- aggregate. ------------------------------------------------------------
    aggregate_node_scores = {
        node.canonical_key: NodeScore(
            canonical_key=node.canonical_key,
            score=running_score[node.canonical_key],
            credit=running_credit[node.canonical_key],
            source=running_source[node.canonical_key],
            best=running_best.get(node.canonical_key),
        )
        for node in ref_nodes
    }
    node_coverage, edge_coverage, winning_path_index = aggregate(
        reference_graph, aggregate_node_scores, tuple(final_edge_scores)
    )

    # --- step 6: new state + snapshot. ------------------------------------------
    pair_count_this_turn = node_pairs + edge_pairs
    new_pair_count_total = state.pair_count_total + pair_count_this_turn
    budget_truncated = new_pair_count_total >= params.max_nli_pairs

    new_state = IncrementalState(
        window_cursor=len(all_student_turns),
        global_window_count=state.global_window_count + len(new_windows),
        running_node_max=dict(running_score),
        node_source=dict(running_source),
        running_edge_evidence=running_edge_evidence,
        seeded_keys=state.seeded_keys,
        pair_count_total=new_pair_count_total,
    )
    snapshot = IncrementalSnapshot(
        node_credits=dict(running_credit),
        edge_scores=tuple(final_edge_scores),
        node_cov=node_coverage,
        edge_cov=edge_coverage,
        winning_path_index=winning_path_index,
        gray=gray_after_upgrade,
        pair_count_this_turn=pair_count_this_turn,
        budget_truncated=budget_truncated,
    )
    return new_state, snapshot
