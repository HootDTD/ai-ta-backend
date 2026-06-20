"""WU-4A1 Task 3 — frozen-DTO contract for the canonical-space types.

These pin the IMMUTABILITY (coding-style: return new objects, never mutate) and
the field shape of the five canonical dataclasses BEFORE any builder is written.
No DB, no LLM, no network.
"""

from __future__ import annotations

import dataclasses

import pytest

from apollo.graph_compare.canonical import (
    CanonicalEdge,
    CanonicalGraph,
    CanonicalNode,
    ReferenceGraph,
    ReferencePathView,
)


def _node() -> CanonicalNode:
    return CanonicalNode(
        canonical_key="eq.continuity",
        node_type="equation",
        source_node_ids=("s1",),
        evidence_spans=("rho*A1*v1 - rho*A2*v2",),
    )


def test_canonical_node_is_frozen():
    node = _node()
    with pytest.raises(dataclasses.FrozenInstanceError):
        node.canonical_key = "eq.bernoulli"  # type: ignore[misc]


def test_canonical_edge_is_frozen():
    edge = CanonicalEdge(
        edge_type="PRECEDES", from_key="proc.a", to_key="proc.b", provenance="explicit"
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        edge.from_key = "proc.c"  # type: ignore[misc]


def test_canonical_graph_is_frozen():
    graph = CanonicalGraph(nodes=(_node(),), edges=(), unresolved_nodes=(), dropped_edge_count=0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        graph.dropped_edge_count = 5  # type: ignore[misc]


def test_reference_path_view_is_frozen():
    view = ReferencePathView(canonical_keys=("eq.continuity",))
    with pytest.raises(dataclasses.FrozenInstanceError):
        view.canonical_keys = ()  # type: ignore[misc]


def test_reference_graph_is_frozen():
    graph = ReferenceGraph(nodes=(_node(),), edges=(), paths=())
    with pytest.raises(dataclasses.FrozenInstanceError):
        graph.nodes = ()  # type: ignore[misc]


def test_canonical_node_defaults():
    """symbolic / method / confidence default to None; evidence/source default
    to empty tuples where applicable."""
    node = CanonicalNode(
        canonical_key="cond.incompressibility",
        node_type="condition",
        source_node_ids=("s1",),
        evidence_spans=("density is constant",),
    )
    assert node.symbolic is None
    assert node.method is None
    assert node.confidence is None


def test_canonical_graph_field_shape():
    """The CanonicalGraph carries the four S_norm fields with the exact types
    WU-4A2 consumes: nodes/edges tuples, unresolved_nodes tuple of (id, text)
    pairs, and an int drop count."""
    node = _node()
    edge = CanonicalEdge(
        edge_type="USES", from_key="proc.a", to_key="eq.continuity", provenance="explicit"
    )
    graph = CanonicalGraph(
        nodes=(node,),
        edges=(edge,),
        unresolved_nodes=(("s9", "some unparsed surface text"),),
        dropped_edge_count=2,
    )
    assert graph.nodes == (node,)
    assert graph.edges == (edge,)
    assert graph.unresolved_nodes == (("s9", "some unparsed surface text"),)
    assert graph.dropped_edge_count == 2


def test_reference_graph_field_shape():
    node = _node()
    view = ReferencePathView(canonical_keys=("eq.continuity", "eq.bernoulli"))
    graph = ReferenceGraph(nodes=(node,), edges=(), paths=(view,))
    assert graph.paths == (view,)
    assert graph.paths[0].canonical_keys == ("eq.continuity", "eq.bernoulli")
