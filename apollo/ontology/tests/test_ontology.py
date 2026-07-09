"""Unit tests for the ontology layer (no Neo4j required)."""
from __future__ import annotations

import pytest

from apollo.ontology import (
    EDGE_ALLOWED_PAIRS,
    NODE_LABELS,
    Edge,
    EdgeType,
    KGGraph,
    build_node,
)


def _equation(node_id: str, attempt_id: int = -1) -> object:
    return build_node(
        node_type="equation",
        node_id=node_id,
        attempt_id=attempt_id,
        source="reference",
        content={"symbolic": "x - y", "label": node_id},
    )


def _procedure(node_id: str, attempt_id: int = -1) -> object:
    return build_node(
        node_type="procedure_step",
        node_id=node_id,
        attempt_id=attempt_id,
        source="reference",
        content={"action": f"do {node_id}", "purpose": "p"},
    )


def test_node_labels_cover_all_six_types():
    assert set(NODE_LABELS) == {
        "equation", "condition", "simplification",
        "definition", "variable_mapping", "procedure_step",
    }


def test_edge_pair_validator_accepts_uses():
    e = Edge(
        edge_type=EdgeType.USES,
        from_node_id="p1", to_node_id="eq1",
        attempt_id=-1, source="reference",
        from_node_type="procedure_step", to_node_type="equation",
    )
    assert e.edge_type == EdgeType.USES


def test_edge_pair_validator_rejects_uses_wrong_direction():
    with pytest.raises(ValueError, match="not allowed"):
        Edge(
            edge_type=EdgeType.USES,
            from_node_id="eq1", to_node_id="p1",
            attempt_id=-1, source="reference",
            from_node_type="equation", to_node_type="procedure_step",
        )


def test_edge_pair_validator_rejects_self_loop():
    with pytest.raises(ValueError, match="self-loop"):
        Edge(
            edge_type=EdgeType.DEPENDS_ON,
            from_node_id="x", to_node_id="x",
            attempt_id=-1, source="reference",
            from_node_type="equation", to_node_type="equation",
        )


def test_kg_graph_neighbors():
    g = KGGraph(
        nodes=[_procedure("p1"), _equation("eq1")],
        edges=[Edge(
            edge_type=EdgeType.USES,
            from_node_id="p1", to_node_id="eq1",
            attempt_id=-1, source="reference",
            from_node_type="procedure_step", to_node_type="equation",
        )],
    )
    neighbors = g.neighbors("p1", EdgeType.USES)
    assert len(neighbors) == 1
    assert neighbors[0].node_id == "eq1"


def test_kg_graph_precedes_chain_walks_in_order():
    g = KGGraph(
        nodes=[_procedure("p3"), _procedure("p1"), _procedure("p2")],
        edges=[
            Edge(edge_type=EdgeType.PRECEDES, from_node_id="p1", to_node_id="p2",
                 attempt_id=-1, source="reference",
                 from_node_type="procedure_step", to_node_type="procedure_step"),
            Edge(edge_type=EdgeType.PRECEDES, from_node_id="p2", to_node_id="p3",
                 attempt_id=-1, source="reference",
                 from_node_type="procedure_step", to_node_type="procedure_step"),
        ],
    )
    chain = g.precedes_chain()
    assert [n.node_id for n in chain] == ["p1", "p2", "p3"]


def test_kg_graph_topological_order_detects_cycle():
    g = KGGraph(
        nodes=[_procedure("p1"), _procedure("p2")],
        edges=[
            Edge(edge_type=EdgeType.PRECEDES, from_node_id="p1", to_node_id="p2",
                 attempt_id=-1, source="reference",
                 from_node_type="procedure_step", to_node_type="procedure_step"),
            Edge(edge_type=EdgeType.PRECEDES, from_node_id="p2", to_node_id="p1",
                 attempt_id=-1, source="reference",
                 from_node_type="procedure_step", to_node_type="procedure_step"),
        ],
    )
    with pytest.raises(ValueError, match="cycle"):
        g.topological_order(EdgeType.PRECEDES, node_type="procedure_step")
