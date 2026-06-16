"""WU-3C2 — structural propagation, neighborhood corroboration, type-compat veto.

§5 steps 2-3: content tiers seed anchor matches; structure narrows the
remainder and modifies confidence. Edges SUGGEST candidates (anchor neighbors
are prioritized) but never ASSERT a match by position alone — student graphs are
edge-sparse, so anchoring identity on edges would invert the reliability order.

Three roles:
- :func:`type_compatible` — the HARD constraint (a student node only resolves
  to a candidate of the SAME node type; condition <-> condition only).
- :func:`neighborhood_boost` — agreement among graph-neighbors raises a match's
  confidence; an incoherent neighbor mapping (negative agreement) never raises
  it (the structural-incoherence veto, applied as a non-boost).
- :func:`anchor_neighbor_ids` — the edge-neighbors of an anchored node, surfaced
  for prioritization.

All pure + immutable: :func:`neighborhood_boost` returns a NEW
:class:`ScoredMatch`, never mutates its input.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from apollo.ontology.graph import KGGraph
from apollo.ontology.nodes import NodeType
from apollo.resolution.candidates import Candidate

# Per-agreeing-neighbor confidence increment (kept small so a corroborated
# match edges ahead of an isolated one without ever flipping the method cap —
# the §3 cap is re-applied downstream by the resolver).
_BOOST_PER_NEIGHBOR = 0.02


@dataclass(frozen=True)
class ScoredMatch:
    """A student-node -> candidate match with a working score, before the
    final method-cap is applied. Immutable."""

    node_id: str
    candidate: Candidate
    method: str
    score: float


def type_compatible(student_node_type: NodeType, candidate: Candidate) -> bool:
    """HARD type constraint (§5): a student node only resolves to a candidate of
    the SAME node type. No cross-type resolution, ever — a condition never
    resolves to an equation candidate even at the top text score."""
    return student_node_type == candidate.node_type


def neighborhood_boost(match: ScoredMatch, *, agreeing_neighbors: int) -> ScoredMatch:
    """Return a NEW :class:`ScoredMatch` whose score is raised by the count of
    graph-neighbors that corroborate the mapping.

    A non-positive ``agreeing_neighbors`` (no corroboration / an incoherent
    neighbor mapping) applies NO boost — the structural-incoherence veto never
    lifts a match above its isolated score."""
    if agreeing_neighbors <= 0:
        return match
    boosted = min(1.0, match.score + _BOOST_PER_NEIGHBOR * agreeing_neighbors)
    return replace(match, score=boosted)


def anchor_neighbor_ids(graph: KGGraph, anchor_node_id: str) -> set[str]:
    """The node ids directly connected to ``anchor_node_id`` (either direction).

    These are the candidates the resolver prioritizes once ``anchor_node_id``
    has matched a reference node — edges suggest, never assert."""
    out: set[str] = set()
    for edge in graph.outgoing(anchor_node_id):
        out.add(edge.to_node_id)
    for edge in graph.incoming(anchor_node_id):
        out.add(edge.from_node_id)
    return out
