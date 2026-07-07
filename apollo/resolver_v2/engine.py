"""Resolver V2 engine — pure orchestration of the §4 chain (task T7).

Runs the design's step-8b sequence over already-loaded inputs:

    build_windows -> load_views/build_ref_nodes -> score_nodes ->
    apply_grayzone -> score_edges -> (edge pull-up floors) -> aggregate

Everything with a side effect is INJECTED: the NLI adjudicator (``None`` =
deterministic lexical-only degrade), the gray-zone checker (``None`` =
disabled, the ``APOLLO_RESOLVER_V2_GRAYZONE=0`` default), and — one defaulted
extension over the card's pinned signature — the window selector
(``select_fn``, default = the real T3 ``prefilter.select_windows``; tests
inject a fake, per the card's own smoke-test contract). No DB, no Neo4j, no
env reads: ``params`` arrives resolved (``config.load_params()`` is the
integration layer's job), so the engine is call-for-call deterministic given
its injected callables.

Ordering contracts honored here (the T4/T5/T6 interlock):

* gray-zone upgrades land BEFORE edges are scored, so edge gates/formulas see
  the upgraded endpoint credits (§4: "pull-ups applied to node credits AFTER
  grayzone" — grayzone first, then edges, then pull-ups);
* T5's returned pull-up floors are applied to node credits by THIS module,
  BEFORE aggregation (T5 scores edges on input credits only);
* the per-attempt NLI pair budget (``params.max_nli_pairs``) is threaded
  across nodes AND edges: edges get whatever the node pass left over.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from apollo.graph_compare.canonical import ReferenceGraph
from apollo.resolution.nli_adjudicator import NLIAdjudicator
from apollo.resolver_v2.aggregate import aggregate
from apollo.resolver_v2.config import ResolverV2Params
from apollo.resolver_v2.edges import score_edges
from apollo.resolver_v2.grayzone import GrayzoneFn, apply_grayzone
from apollo.resolver_v2.prefilter import select_windows
from apollo.resolver_v2.scoring import credit_for_score, score_nodes
from apollo.resolver_v2.types import NodeScore, RefNode, ResolverV2Result, SelectFn
from apollo.resolver_v2.views import build_ref_nodes, load_views
from apollo.resolver_v2.windows import build_windows

#: ``NodeScore.source`` values a gray-zone upgrade may replace. ``v1_floor``
#: is deliberately absent (credit is already 1.0 — never downgrade), and
#: ``zero`` nodes have score 0.0 which can never sit in the gray band.
_GRAY_UPGRADABLE_SOURCES: frozenset[str] = frozenset({"nli", "lexical_skip"})


def _gray_nodes(
    node_scores: Sequence[NodeScore], params: ResolverV2Params
) -> tuple[NodeScore, ...]:
    """The §6 gray band: nodes whose fused score landed in
    ``t_low <= score < t_mid`` AND whose credit was not already floored by v1
    (a v1-resolved node keeps 1.0; querying it could only no-op or confuse the
    trace)."""
    gray: list[NodeScore] = []
    for node in node_scores:
        if node.source not in _GRAY_UPGRADABLE_SOURCES:
            continue
        _credit, is_gray = credit_for_score(node.score, params)
        if is_gray:
            gray.append(node)
    return tuple(gray)


def _apply_upgrades(
    node_scores: tuple[NodeScore, ...],
    upgrades: dict[str, float],
    *,
    source: str,
) -> tuple[NodeScore, ...]:
    """Only-lift credit rewrite: a node in ``upgrades`` whose new credit is
    STRICTLY higher takes it (``source`` records the winner); everything else
    is unchanged. Order-preserving; never lowers a credit."""
    if not upgrades:
        return node_scores
    out: list[NodeScore] = []
    for node in node_scores:
        new_credit = upgrades.get(node.canonical_key)
        if new_credit is not None and new_credit > node.credit:
            out.append(replace(node, credit=new_credit, source=source))
        else:
            out.append(node)
    return tuple(out)


def run_resolver_v2(
    *,
    student_turns: Sequence[str],
    reference_graph: ReferenceGraph,
    problem_payload: dict,
    v1_resolved_keys: frozenset[str],
    v1_explicit_triples: frozenset[tuple[str, str, str]],
    v1_inferred_triples: frozenset[tuple[str, str, str]],
    nli: NLIAdjudicator | None,
    grayzone_fn: GrayzoneFn | None,
    params: ResolverV2Params,
    select_fn: SelectFn = select_windows,
) -> ResolverV2Result:
    """Run the full V2 scoring chain and return the frozen result whose
    ``node_coverage``/``edge_coverage`` are the exact numbers
    ``integration.substitute_scores`` writes into ``GradeResult``.

    ``student_turns`` must be role-filtered student turn texts in
    ``turn_index`` order (``integration.load_student_turns``). Views come from
    the committed cache keyed by the payload's ``concept_id``/``id`` — a
    missing cache entry degrades to label-only views (never raises).
    """
    # §5.1 windows + §5.2 hypothesis views.
    windows = build_windows(student_turns, params)
    concept_id = str(problem_payload.get("concept_id") or "")
    problem_id = str(problem_payload.get("id") or "")
    views_by_key = load_views(concept_id, problem_id)
    ref_nodes: tuple[RefNode, ...] = build_ref_nodes(
        reference_graph, problem_payload, views_by_key
    )

    # §5.3-§5.5 node scoring (v1 floor applied inside).
    node_scores, node_pairs = score_nodes(
        windows,
        ref_nodes,
        nli=nli,
        params=params,
        v1_resolved_keys=v1_resolved_keys,
        select_fn=select_fn,
    )

    # §6 gray-zone upgrade (one batched call; fn None = deterministic 0.3).
    gray = _gray_nodes(node_scores, params)
    grayzone_used = grayzone_fn is not None and bool(gray)
    if grayzone_used:
        transcript = "\n".join(student_turns)
        ref_by_key = {ref.canonical_key: ref for ref in ref_nodes}
        upgrades = apply_grayzone(
            gray, transcript, grayzone_fn, params, ref_nodes=ref_by_key
        )
        node_scores = _apply_upgrades(node_scores, upgrades, source="grayzone")

    # §5.6 graded edges — scored on the post-grayzone credits, with whatever
    # NLI budget the node pass left over.
    node_by_key = {node.canonical_key: node for node in node_scores}
    labels = {ref.canonical_key: ref.label for ref in ref_nodes}
    edge_scores, pullup_floors, edge_pairs = score_edges(
        reference_graph.edges,
        node_by_key,
        windows,
        labels=labels,
        nli=nli,
        params=params,
        v1_explicit_triples=v1_explicit_triples,
        v1_inferred_triples=v1_inferred_triples,
        select_fn=select_fn,
        pair_budget_left=max(0, params.max_nli_pairs - node_pairs),
    )

    # §5.6 node pull-up: apply T5's returned floors to node credits BEFORE
    # aggregation (the engine's job per the T5 contract).
    node_scores = _apply_upgrades(node_scores, pullup_floors, source="edge_pullup")
    node_by_key = {node.canonical_key: node for node in node_scores}

    # §5.7 winning-path node coverage + all-edges edge coverage.
    node_coverage, edge_coverage, winning_path_index = aggregate(
        reference_graph, node_by_key, edge_scores
    )

    return ResolverV2Result(
        node_scores=node_scores,
        edge_scores=edge_scores,
        node_coverage=node_coverage,
        edge_coverage=edge_coverage,
        winning_path_index=winning_path_index,
        grayzone_used=grayzone_used,
        pair_count=node_pairs + edge_pairs,
    )
