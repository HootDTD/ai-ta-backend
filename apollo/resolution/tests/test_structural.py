"""WU-3C2 Step 5 — pure-unit tests for structural propagation + type-compat veto.

No Docker, no network. Pin (§5 steps 2-3):
- type compatibility is a HARD constraint (condition never resolves to an
  equation candidate, even at top text score);
- a neighbor-corroborated mapping scores higher than the same match in
  isolation (neighborhood agreement boosts confidence);
- a structurally incoherent candidate is vetoed;
- anchor neighbors are surfaced for prioritization.
"""

from __future__ import annotations

from apollo.ontology.edges import Edge, EdgeType
from apollo.ontology.graph import KGGraph
from apollo.ontology.nodes import build_node
from apollo.resolution.candidates import Candidate
from apollo.resolution.structural import (
    ScoredMatch,
    neighborhood_boost,
    type_compatible,
)


def _cand(key, *, node_type):
    return Candidate(
        canonical_key=key,
        canon_key=1,
        node_type=node_type,
        is_misconception=False,
        symbolic=None,
        aliases=(),
        display_name=key,
        opposes_key=None,
    )


def _node(node_id, node_type, **content):
    defaults = {
        "equation": {"symbolic": "P1 = P2", "label": "", "variables": []},
        "condition": {"applies_when": "density is constant", "label": ""},
        "procedure_step": {"action": "do a thing", "purpose": ""},
    }
    return build_node(
        node_type=node_type,
        node_id=node_id,
        attempt_id=1,
        source="parser",
        content=content or defaults[node_type],
    )


def test_type_compatibility_hard_constraint():
    """A condition student node must NEVER resolve to an equation candidate,
    regardless of text score."""
    assert type_compatible("condition", _cand("eq.bernoulli", node_type="equation")) is False
    assert type_compatible("condition", _cand("cond.x", node_type="condition")) is True


def test_neighborhood_agreement_boosts_confidence():
    """A mapping whose graph-neighbors also resolve to the candidate's
    neighbors scores higher than the same match in isolation."""
    isolated = ScoredMatch(
        node_id="s1",
        candidate=_cand("eq.bernoulli", node_type="equation"),
        method="fuzzy",
        score=0.90,
    )
    corroborated = neighborhood_boost(isolated, agreeing_neighbors=2)
    alone = neighborhood_boost(isolated, agreeing_neighbors=0)
    assert corroborated.score > alone.score


def test_structurally_incoherent_mapping_vetoed():
    """neighborhood_boost with a NEGATIVE agreement count (an incoherent
    neighbor mapping) does not raise the score above the base."""
    base = ScoredMatch(
        node_id="s1",
        candidate=_cand("eq.bernoulli", node_type="equation"),
        method="fuzzy",
        score=0.90,
    )
    incoherent = neighborhood_boost(base, agreeing_neighbors=-1)
    assert incoherent.score <= base.score


def test_anchor_neighbors_surfaced_for_prioritization():
    """Given an anchored student node, its edge-neighbors are returned so the
    resolver can prioritize them (edges suggest candidates, never assert)."""
    from apollo.resolution.structural import anchor_neighbor_ids

    n1 = _node("s1", "procedure_step")
    n2 = _node("s2", "equation")
    graph = KGGraph(
        nodes=[n1, n2],
        edges=[
            Edge(
                edge_type=EdgeType.USES,
                from_node_id="s1",
                to_node_id="s2",
                attempt_id=1,
                from_node_type="procedure_step",
                to_node_type="equation",
            )
        ],
    )
    assert anchor_neighbor_ids(graph, "s1") == {"s2"}


def test_anchor_neighbors_include_incoming_edges():
    """Neighbors are collected in BOTH directions — an incoming edge's source
    is surfaced too."""
    from apollo.resolution.structural import anchor_neighbor_ids

    n1 = _node("s1", "procedure_step")
    n2 = _node("s2", "equation")
    graph = KGGraph(
        nodes=[n1, n2],
        edges=[
            Edge(
                edge_type=EdgeType.USES,
                from_node_id="s1",
                to_node_id="s2",
                attempt_id=1,
                from_node_type="procedure_step",
                to_node_type="equation",
            )
        ],
    )
    # From s2's perspective, s1 is an INCOMING neighbor.
    assert anchor_neighbor_ids(graph, "s2") == {"s1"}
