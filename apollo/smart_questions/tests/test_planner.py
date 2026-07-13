from apollo.ontology import Edge, EdgeType, KGGraph, build_node
from apollo.smart_questions.planner import NodeCoverage, choose_target


def _graph() -> KGGraph:
    first = build_node(
        node_type="definition",
        node_id="first",
        attempt_id=1,
        source="reference",
        content={"concept": "x", "meaning": "first meaning"},
    )
    second = build_node(
        node_type="procedure_step",
        node_id="second",
        attempt_id=1,
        source="reference",
        content={"action": "apply it", "purpose": "finish"},
    )
    edge = Edge(
        edge_type=EdgeType.DEPENDS_ON,
        from_node_id="first",
        to_node_id="second",
        attempt_id=1,
        source="reference",
        from_node_type="definition",
        to_node_type="procedure_step",
    )
    return KGGraph(nodes=[first, second], edges=[edge])


def test_selects_uncovered_prerequisite_first():
    coverage = [
        NodeCoverage("first", "missing", 0.0),
        NodeCoverage("second", "missing", 0.0),
    ]
    assert choose_target(_graph(), coverage, set()) == "first"


def test_never_reasks_an_attempted_node():
    coverage = [
        NodeCoverage("first", "missing", 0.0),
        NodeCoverage("second", "missing", 0.0),
    ]
    assert choose_target(_graph(), coverage, {"first"}) == "second"


def test_stops_when_covered_or_every_gap_was_asked():
    covered = [
        NodeCoverage("first", "covered", 1.0),
        NodeCoverage("second", "covered", 0.8),
    ]
    assert choose_target(_graph(), covered, set()) is None

    missing = [
        NodeCoverage("first", "missing", 0.0),
        NodeCoverage("second", "partial", 0.4),
    ]
    assert choose_target(_graph(), missing, {"first", "second"}) is None
