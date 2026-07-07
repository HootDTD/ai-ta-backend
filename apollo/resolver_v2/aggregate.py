"""Resolver V2 aggregation — winning-path node coverage + graded edge
coverage (design §5.7, task T5).

Mirrors ``apollo/graph_compare/coverage.py``'s per-path MAX semantics — a
student who took a valid alternative path is graded against the path they
actually took — but over GRADED credits instead of set membership:

    node_cov(path) = mean over path keys k of credit(k)
    node_coverage  = max over reference.paths of node_cov(path)
                     (ties -> lowest path_index, exactly v1's winner rule)
    edge_coverage  = mean edge credit over ALL reference edges
                     (v1 denominator parity)

On a binary-credit input (credit ∈ {0, 1}) this reproduces v1's
set-membership math bit-for-bit: same winning path, same score.

Pure + deterministic; never NaN / ZeroDivision (every mean is guarded, empty
cases score vacuous 1.0 per v1 parity).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from apollo.graph_compare.canonical import ReferenceGraph
from apollo.resolver_v2.types import EdgeScore, NodeScore


def _node_credit(node_scores: Mapping[str, NodeScore], key: str) -> float:
    """Credit for one reference key; absent from ``node_scores`` -> 0.0
    (defensive — the engine scores every union-of-paths key)."""
    node = node_scores.get(key)
    return node.credit if node is not None else 0.0


def _path_score(node_scores: Mapping[str, NodeScore], keys: Sequence[str]) -> float:
    """Mean credit over one declared path's keys. A degenerate empty path
    scores vacuous 1.0 (v1 ``coverage_per_path`` parity — never a
    ZeroDivision)."""
    if not keys:
        return 1.0
    return sum(_node_credit(node_scores, key) for key in keys) / len(keys)


def aggregate(
    reference_graph: ReferenceGraph,
    node_scores: Mapping[str, NodeScore],
    edge_scores: Sequence[EdgeScore],
) -> tuple[float, float, int]:
    """Return ``(node_coverage, edge_coverage, winning_path_index)`` (§5.7).

    - ``node_coverage``: max over declared paths of the path's mean node
      credit; ties broken by the lowest ``path_index`` (identical to v1
      ``coverage.py`` ``_select_winning_path`` — ``min`` on
      ``(-score, path_index)``).
    - ``edge_coverage``: mean credit over ``edge_scores`` (one per reference
      edge, in reference order, per the T5 ``score_edges`` contract); zero
      reference edges -> vacuous 1.0 (v1 parity).
    - ``winning_path_index``: index into ``reference_graph.paths`` of the
      winner. ``paths`` is guaranteed >= 1 upstream (``validate_reference``);
      the empty-paths guard is defensive only (vacuous 1.0, index 0).
    """
    path_scores: tuple[tuple[float, int], ...] = tuple(
        (_path_score(node_scores, path.canonical_keys), path_index)
        for path_index, path in enumerate(reference_graph.paths)
    )
    if path_scores:
        node_coverage, winning_path_index = min(
            path_scores, key=lambda sp: (-sp[0], sp[1])
        )
    else:  # defensive: validate_reference guarantees >= 1 declared path
        node_coverage, winning_path_index = 1.0, 0

    if edge_scores:
        edge_coverage = sum(edge.credit for edge in edge_scores) / len(edge_scores)
    else:
        edge_coverage = 1.0  # vacuous — no reference edges to teach (v1 parity)

    return (node_coverage, edge_coverage, winning_path_index)
