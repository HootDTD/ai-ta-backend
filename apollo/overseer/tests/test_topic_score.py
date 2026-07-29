from __future__ import annotations

from apollo.ontology import Edge, EdgeType, KGGraph, build_node
from apollo.overseer.topic_score import compute_centrality, compute_topic_score


def _equation(node_id: str, label: str):
    return build_node(
        node_type="equation",
        node_id=node_id,
        attempt_id=1,
        source="reference",
        content={"symbolic": "x = y", "label": label},
    )


def _procedure(node_id: str):
    return build_node(
        node_type="procedure_step",
        node_id=node_id,
        attempt_id=1,
        source="reference",
        content={"action": node_id},
    )


def test_topic_payload_retains_empty_misconceptions_after_detector_removal():
    nodes = [_equation("eq.one", "One"), _equation("eq.two", "Two")]
    graph = KGGraph(nodes=nodes)
    result = compute_topic_score(
        coverage={"per_step": {"eq.one": "covered", "eq.two": "missing"}},
        reference_nodes=nodes,
        centrality=compute_centrality(graph),
    )

    assert result.score == 50
    assert result.misconception_dock == 0.0
    assert [topic.canonical_key for topic in result.topics] == ["eq.one", "eq.two"]
    assert all(topic.misconceptions == () for topic in result.topics)


def test_topic_score_preserves_partial_credit_and_evidence_span():
    node = _equation("eq.one", "One")
    result = compute_topic_score(
        coverage={
            "per_step": {"eq.one": "missing"},
            "procedure_scores": {"eq.one": 0.4},
        },
        reference_nodes=[node],
        centrality={"eq.one": 1.0},
        evidence_spans={"eq.one": "my own words"},
    )

    assert result.score == 40
    assert result.topics[0].status == "partial"
    assert result.topics[0].evidence_span == "my own words"


def test_empty_reference_graph_degrades_to_empty_f_payload():
    result = compute_topic_score(coverage={}, reference_nodes=[], centrality={})
    assert result.score == 0
    assert result.letter == "F"
    assert result.topics == ()


def test_centrality_covers_singleton_and_structural_edges():
    single = _equation("eq.one", "One")
    assert compute_centrality(KGGraph(nodes=[single])) == {"eq.one": 1.0}

    first, second, third = (_procedure("p1"), _procedure("p2"), _procedure("p3"))
    graph = KGGraph(
        nodes=[first, second, third],
        edges=[
            Edge(
                edge_type=EdgeType.DEPENDS_ON,
                from_node_id="p1",
                to_node_id="p2",
                attempt_id=1,
            ),
            Edge(
                edge_type=EdgeType.PRECEDES,
                from_node_id="p1",
                to_node_id="p2",
                attempt_id=1,
                from_node_type="procedure_step",
                to_node_type="procedure_step",
            ),
            Edge(
                edge_type=EdgeType.PRECEDES,
                from_node_id="p2",
                to_node_id="p3",
                attempt_id=1,
                from_node_type="procedure_step",
                to_node_type="procedure_step",
            ),
        ],
    )
    centrality = compute_centrality(graph)
    assert centrality["p1"] == 1.0
    assert centrality["p1"] > centrality["p2"] > centrality["p3"]


def test_centrality_precedes_cycle_falls_back_without_raising():
    first, second = _procedure("p1"), _procedure("p2")
    edges = [
        Edge(
            edge_type=EdgeType.PRECEDES,
            from_node_id=source,
            to_node_id=target,
            attempt_id=1,
            from_node_type="procedure_step",
            to_node_type="procedure_step",
        )
        for source, target in (("p1", "p2"), ("p2", "p1"))
    ]
    assert compute_centrality(KGGraph(nodes=[first, second], edges=edges)) == {
        "p1": 0.3,
        "p2": 0.3,
    }
