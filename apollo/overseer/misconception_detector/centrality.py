"""Pure reference-graph centrality for the Apollo misconception detector.

Frozen contract: ``docs/_archive/plans/2026-07-08-apollo-misconception-detector-plan.md``
section 8 (T2), amended by A6 (cycle safety).

``compute_centrality`` scores how "central" each reference-graph node is, so
``merge.py`` can weight a corroborated misconception finding by how load-
bearing the concept it attacks is (``severity = centrality * confidence``).
Two structural signals feed the score:

  * **DEPENDS_ON in-degree** — a node many other nodes depend on is more
    central than a leaf nobody points at.
  * **PRECEDES position** — earlier steps in a procedure chain are more
    central than later ones (an error early in a procedure poisons
    everything downstream).

Both signals are combined and rescaled into ``[CENTRALITY_W_MIN, 1.0]`` so a
peripheral node still carries a nonzero weight (A conservative floor per
config.py's calibration doc — a detector must never zero out a genuine
misconception simply because its concept sits at the edge of the graph).

This module is pure graph math: no IO, no LLM, no DB. It must never raise —
a raised exception here would crash the whole grade (A6). The only bounded
external call, ``KGGraph.topological_order``, raises ``ValueError`` on a
cyclic subgraph; we catch that and fall back to a DEPENDS_ON-only (or
uniform, if that too is empty) centrality rather than propagating the error.
"""

from __future__ import annotations

from apollo.ontology import EdgeType, KGGraph
from apollo.overseer.misconception_detector.config import CENTRALITY_W_MIN


def compute_centrality(reference_graph: KGGraph) -> dict[str, float]:
    """Score every node in ``reference_graph`` in ``[CENTRALITY_W_MIN, 1.0]``.

    Returns a NEW dict (never mutates the input graph). Empty graph -> ``{}``.
    Single-node graph -> that node scores ``1.0``.
    """
    node_ids = [n.node_id for n in reference_graph.nodes]
    if not node_ids:
        return {}
    if len(node_ids) == 1:
        return {node_ids[0]: 1.0}

    depends_on_scores = _depends_on_in_degree_scores(reference_graph, node_ids)
    precedes_scores = _precedes_position_scores(reference_graph, node_ids)

    # Combine additively (not via max()): a node with a real DEPENDS_ON
    # in-degree signal must never be masked by an incidental PRECEDES
    # position assigned to unrelated, edge-less nodes (or vice versa). Each
    # sub-score is already scaled to [0, 1], so summing and clamping keeps
    # the combined raw score in [0, 1] while letting both signals count.
    combined = {
        nid: min(
            1.0,
            depends_on_scores.get(nid, 0.0) + precedes_scores.get(nid, 0.0),
        )
        for nid in node_ids
    }
    return _rescale(combined, node_ids)


def _depends_on_in_degree_scores(graph: KGGraph, node_ids: list[str]) -> dict[str, float]:
    """Raw (unscaled, [0,1]) in-degree signal over the DEPENDS_ON subgraph."""
    in_degree = {nid: 0 for nid in node_ids}
    for edge in graph.edges:
        if edge.edge_type != EdgeType.DEPENDS_ON:
            continue
        if edge.to_node_id in in_degree:
            in_degree[edge.to_node_id] += 1

    max_degree = max(in_degree.values(), default=0)
    if max_degree == 0:
        return {nid: 0.0 for nid in node_ids}
    return {nid: degree / max_degree for nid, degree in in_degree.items()}


def _precedes_position_scores(graph: KGGraph, node_ids: list[str]) -> dict[str, float]:
    """Raw (unscaled, [0,1]) position signal over the PRECEDES subgraph.

    Earlier positions in the topological order score higher. Falls back to a
    uniform (all-zero-signal, i.e. no differentiation) score on a cyclic
    PRECEDES subgraph (A6) rather than raising.
    """
    # No real PRECEDES edges at all -> no positional signal to contribute.
    # (Without this guard, topological_order would still trivially "order"
    # every disconnected node via Kahn's queue, handing out an arbitrary
    # position-based score that has no basis in the actual graph shape.)
    precedes_edges = [e for e in graph.edges if e.edge_type == EdgeType.PRECEDES]
    if not precedes_edges:
        return {nid: 0.0 for nid in node_ids}

    try:
        ordered = graph.topological_order(EdgeType.PRECEDES)
    except ValueError:
        return {nid: 0.0 for nid in node_ids}

    # Restrict to nodes actually touched by a PRECEDES edge; untouched nodes
    # (e.g. equations in a mixed graph) keep a 0.0 positional contribution.
    chain_ids = {e.from_node_id for e in precedes_edges} | {
        e.to_node_id for e in precedes_edges
    }
    ordered_ids = [n.node_id for n in ordered if n.node_id in chain_ids]
    scores = {nid: 0.0 for nid in node_ids}
    if len(ordered_ids) <= 1:
        return scores

    last_index = len(ordered_ids) - 1
    for position, nid in enumerate(ordered_ids):
        # First node -> 1.0, last node -> 0.0, linear in between.
        scores[nid] = 1.0 - (position / last_index)
    return scores


def _rescale(raw_scores: dict[str, float], node_ids: list[str]) -> dict[str, float]:
    """Map raw [0,1] combined scores into [CENTRALITY_W_MIN, 1.0]."""
    span = 1.0 - CENTRALITY_W_MIN
    return {nid: CENTRALITY_W_MIN + raw_scores.get(nid, 0.0) * span for nid in node_ids}
