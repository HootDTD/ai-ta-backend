"""RED tests for the misconception-detector reference-graph centrality (T2).

Frozen contract: ``docs/_archive/plans/2026-07-08-apollo-misconception-detector-plan.md``
section 8 (T2 assertions), amended by A6 (cycle safety).

``compute_centrality`` is pure graph math over a reference ``KGGraph`` — no
IO, no LLM, no DB. It must never raise, even on a malformed (cyclic)
PRECEDES subgraph, because a raised exception here would crash the grade.
"""

from __future__ import annotations

from apollo.ontology import Edge, EdgeType, KGGraph, build_node
from apollo.overseer.misconception_detector.centrality import compute_centrality
from apollo.overseer.misconception_detector.config import CENTRALITY_W_MIN


def _equation(node_id: str, attempt_id: int = 1) -> object:
    return build_node(
        node_type="equation",
        node_id=node_id,
        attempt_id=attempt_id,
        source="reference",
        content={"symbolic": "x=1", "label": node_id, "variables": ["x"]},
    )


def _procedure_step(node_id: str, attempt_id: int = 1) -> object:
    return build_node(
        node_type="procedure_step",
        node_id=node_id,
        attempt_id=attempt_id,
        source="reference",
        content={"action": f"do {node_id}", "purpose": ""},
    )


def _depends_on(from_id: str, to_id: str, attempt_id: int = 1) -> Edge:
    return Edge(
        edge_type=EdgeType.DEPENDS_ON,
        from_node_id=from_id,
        to_node_id=to_id,
        attempt_id=attempt_id,
        from_node_type="equation",
        to_node_type="equation",
    )


def _precedes(from_id: str, to_id: str, attempt_id: int = 1) -> Edge:
    return Edge(
        edge_type=EdgeType.PRECEDES,
        from_node_id=from_id,
        to_node_id=to_id,
        attempt_id=attempt_id,
        from_node_type="procedure_step",
        to_node_type="procedure_step",
    )


class TestDependsOnCentrality:
    def test_node_with_more_incoming_depends_on_scores_higher_than_leaf(self):
        # hub <- a, hub <- b, hub <- c ; leaf has no incoming edges at all.
        hub = _equation("hub")
        a, b, c = _equation("a"), _equation("b"), _equation("c")
        leaf = _equation("leaf")
        graph = KGGraph(
            nodes=[hub, a, b, c, leaf],
            edges=[
                _depends_on("a", "hub"),
                _depends_on("b", "hub"),
                _depends_on("c", "hub"),
            ],
        )

        scores = compute_centrality(graph)

        assert scores["hub"] > scores["leaf"]

    def test_more_incoming_edges_strictly_increases_score(self):
        one_incoming = _equation("one")
        two_incoming = _equation("two")
        src_a, src_b, src_c = _equation("sa"), _equation("sb"), _equation("sc")
        graph = KGGraph(
            nodes=[one_incoming, two_incoming, src_a, src_b, src_c],
            edges=[
                _depends_on("sa", "one"),
                _depends_on("sb", "two"),
                _depends_on("sc", "two"),
            ],
        )

        scores = compute_centrality(graph)

        assert scores["two"] > scores["one"]


class TestPrecedesCentrality:
    def test_head_and_tail_scores_differ(self):
        head = _procedure_step("step1")
        mid = _procedure_step("step2")
        tail = _procedure_step("step3")
        graph = KGGraph(
            nodes=[head, mid, tail],
            edges=[
                _precedes("step1", "step2"),
                _precedes("step2", "step3"),
            ],
        )

        scores = compute_centrality(graph)

        assert scores["step1"] != scores["step3"]


class TestSingleNodeGraph:
    def test_single_node_scores_one(self):
        only = _equation("only")
        graph = KGGraph(nodes=[only], edges=[])

        scores = compute_centrality(graph)

        assert scores["only"] == 1.0


class TestScoreBounds:
    def test_all_scores_within_bounds(self):
        hub = _equation("hub")
        a, b = _equation("a"), _equation("b")
        leaf = _equation("leaf")
        head = _procedure_step("h1")
        tail = _procedure_step("h2")
        graph = KGGraph(
            nodes=[hub, a, b, leaf, head, tail],
            edges=[
                _depends_on("a", "hub"),
                _depends_on("b", "hub"),
                _precedes("h1", "h2"),
            ],
        )

        scores = compute_centrality(graph)

        assert len(scores) == 6
        for node_id, score in scores.items():
            assert CENTRALITY_W_MIN <= score <= 1.0, f"{node_id}={score} out of bounds"


class TestCyclicPrecedesSafety:
    """A6 — a cyclic PRECEDES subgraph must not crash the grade."""

    def test_cyclic_precedes_does_not_raise(self):
        s1 = _procedure_step("c1")
        s2 = _procedure_step("c2")
        s3 = _procedure_step("c3")
        graph = KGGraph(
            nodes=[s1, s2, s3],
            edges=[
                _precedes("c1", "c2"),
                _precedes("c2", "c3"),
                _precedes("c3", "c1"),  # closes the cycle
            ],
        )

        # Must not raise ValueError from topological_order.
        scores = compute_centrality(graph)

        assert set(scores.keys()) == {"c1", "c2", "c3"}
        for score in scores.values():
            assert CENTRALITY_W_MIN <= score <= 1.0

    def test_cyclic_precedes_falls_back_to_depends_on_signal(self):
        # Cyclic PRECEDES among steps, PLUS a DEPENDS_ON hub among equations.
        # The fallback must still surface the DEPENDS_ON-driven differentiation
        # rather than raising or zeroing everything out.
        s1 = _procedure_step("c1")
        s2 = _procedure_step("c2")
        hub = _equation("hub")
        a, b = _equation("a"), _equation("b")
        leaf = _equation("leaf")
        graph = KGGraph(
            nodes=[s1, s2, hub, a, b, leaf],
            edges=[
                _precedes("c1", "c2"),
                _precedes("c2", "c1"),  # cycle
                _depends_on("a", "hub"),
                _depends_on("b", "hub"),
            ],
        )

        scores = compute_centrality(graph)

        assert scores["hub"] > scores["leaf"]
        assert CENTRALITY_W_MIN <= scores["c1"] <= 1.0
        assert CENTRALITY_W_MIN <= scores["c2"] <= 1.0


class TestEmptyGraph:
    def test_empty_graph_returns_empty_dict(self):
        graph = KGGraph(nodes=[], edges=[])

        scores = compute_centrality(graph)

        assert scores == {}


class TestDefensiveEdgeCases:
    def test_depends_on_edge_pointing_outside_graph_is_ignored(self):
        # `to_node_id` "ghost" is not among reference_graph.nodes; the
        # in-degree scan must skip it rather than KeyError.
        a = _equation("a")
        b = _equation("b")
        graph = KGGraph(
            nodes=[a, b],
            edges=[_depends_on("a", "ghost"), _depends_on("a", "b")],
        )

        scores = compute_centrality(graph)

        assert set(scores.keys()) == {"a", "b"}
        for score in scores.values():
            assert CENTRALITY_W_MIN <= score <= 1.0

    def test_precedes_chain_with_single_touched_node_has_no_position_signal(self):
        # A PRECEDES edge whose `to_node_id` ("missing") is absent from the
        # graph's own node list: only "only" survives the chain_ids filter
        # to nodes actually present, so ordered_ids collapses to length <= 1
        # and the position signal is uniformly 0.0 rather than dividing by
        # zero or crashing.
        only = _procedure_step("only")
        other_type = _equation("other")
        graph = KGGraph(
            nodes=[only, other_type],
            edges=[
                Edge(
                    edge_type=EdgeType.PRECEDES,
                    from_node_id="only",
                    to_node_id="missing",
                    attempt_id=1,
                    from_node_type="procedure_step",
                    to_node_type="procedure_step",
                )
            ],
        )

        scores = compute_centrality(graph)

        assert set(scores.keys()) == {"only", "other"}
        for score in scores.values():
            assert CENTRALITY_W_MIN <= score <= 1.0
