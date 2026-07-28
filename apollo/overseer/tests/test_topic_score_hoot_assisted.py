"""INTERACTION5 — additive ``TopicCredit.hoot_assisted`` population in
``compute_topic_score``. Absent map -> every topic False and the result is
byte-identical to the pre-feature build (the existing ``test_topic_score.py``
suite stays green untouched)."""

from __future__ import annotations

import pytest

from apollo.ontology import KGGraph, build_node
from apollo.overseer.topic_score import compute_centrality, compute_topic_score

pytestmark = pytest.mark.unit


def _equation(node_id: str, label: str):
    return build_node(
        node_type="equation",
        node_id=node_id,
        attempt_id=1,
        source="reference",
        content={"symbolic": "x = y", "label": label},
    )


def _nodes():
    return [_equation("eq.one", "One"), _equation("eq.two", "Two")]


def test_hoot_assisted_populated_from_coverage_map():
    nodes = _nodes()
    graph = KGGraph(nodes=nodes)
    result = compute_topic_score(
        coverage={
            "per_step": {"eq.one": "missing", "eq.two": "covered"},
            "procedure_scores": {"eq.one": 0.5, "eq.two": 1.0},
            "hoot_assisted": {"eq.one": True, "eq.two": False},
        },
        reference_nodes=nodes,
        centrality=compute_centrality(graph),
    )

    by_key = {t.canonical_key: t for t in result.topics}
    assert by_key["eq.one"].hoot_assisted is True
    assert by_key["eq.two"].hoot_assisted is False


def test_absent_hoot_assisted_map_leaves_every_topic_false():
    nodes = _nodes()
    graph = KGGraph(nodes=nodes)
    result = compute_topic_score(
        coverage={"per_step": {"eq.one": "covered", "eq.two": "missing"}},
        reference_nodes=nodes,
        centrality=compute_centrality(graph),
    )

    assert all(t.hoot_assisted is False for t in result.topics)
    # The additive field is the ONLY difference: score/letter/topics keys are the
    # same values the pre-feature build produced for this coverage.
    assert result.score == 50
    assert [t.canonical_key for t in result.topics] == ["eq.one", "eq.two"]


def test_node_missing_from_hoot_assisted_map_defaults_false():
    nodes = _nodes()
    graph = KGGraph(nodes=nodes)
    result = compute_topic_score(
        coverage={
            "per_step": {"eq.one": "covered", "eq.two": "covered"},
            "procedure_scores": {"eq.one": 1.0, "eq.two": 1.0},
            # Only eq.one carries a flag; eq.two is absent from the map.
            "hoot_assisted": {"eq.one": True},
        },
        reference_nodes=nodes,
        centrality=compute_centrality(graph),
    )

    by_key = {t.canonical_key: t for t in result.topics}
    assert by_key["eq.one"].hoot_assisted is True
    assert by_key["eq.two"].hoot_assisted is False
