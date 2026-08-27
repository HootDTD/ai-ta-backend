"""Shared fixture corpus for the P3.2 dark-ceiling inertness pin.

Kept in its own module (imported by ``test_topic_score_ceiling.py``, never
collected by pytest) so the SAME corpus could be executed against the
pre-feature ``topic_score.py`` at ``origin/staging`` to generate the frozen
digests. It therefore imports only symbols that existed before P3.2 — adding a
P3.2 import here would make the baseline irreproducible.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from apollo.ontology import Edge, EdgeType, KGGraph, build_node
from apollo.overseer.topic_score import compute_centrality
from apollo.overseer.topic_score_serialize import serialize_topic_score


def equation(node_id: str, label: str):
    return build_node(
        node_type="equation",
        node_id=node_id,
        attempt_id=1,
        source="reference",
        content={"symbolic": "x = y", "label": label},
    )


def condition(node_id: str, applies_when: str):
    return build_node(
        node_type="condition",
        node_id=node_id,
        attempt_id=1,
        source="reference",
        content={"applies_when": applies_when, "label": node_id},
    )


def simplification(node_id: str):
    return build_node(
        node_type="simplification",
        node_id=node_id,
        attempt_id=1,
        source="reference",
        content={"applies_when": "friction is negligible", "transformation": "drop it"},
    )


def procedure(node_id: str):
    return build_node(
        node_type="procedure_step",
        node_id=node_id,
        attempt_id=1,
        source="reference",
        content={"action": node_id},
    )


def digest(result: Any) -> str:
    """sha256 of the serialized result — the whole payload, not just the score."""
    return hashlib.sha256(
        json.dumps(serialize_topic_score(result), sort_keys=True).encode("utf-8")
    ).hexdigest()


def inertness_corpus() -> list[tuple[str, dict[str, Any]]]:
    """Representative ``compute_topic_score`` calls, default keywords only.

    Deliberately spans every branch the level-0 path can take: the binary
    ``per_step`` lane, continuous procedure scores, evidence spans, the
    INTERACTION5 assist map, P1.2b's ``unprobed`` exclusion AND its floor
    widening, D2's capped reference-text reveal, a mixed node-type rubric with
    structural centrality, and the empty-graph degenerate result.
    """
    two = [equation("eq.one", "One"), equation("eq.two", "Two")]
    three = [equation("eq.one", "One"), condition("c.one", "steady flow"), procedure("p.one")]
    four = [procedure("p.one"), procedure("p.two"), procedure("p.three"), procedure("p.four")]
    mixed = [
        equation("eq.one", "Bernoulli"),
        condition("c.one", "incompressible"),
        simplification("s.one"),
        procedure("p.one"),
    ]
    chain = KGGraph(
        nodes=mixed,
        edges=[
            Edge(
                from_node_id="c.one",
                to_node_id="eq.one",
                edge_type=EdgeType.DEPENDS_ON,
                attempt_id=1,
            ),
            Edge(
                from_node_id="p.one",
                to_node_id="s.one",
                edge_type=EdgeType.PRECEDES,
                attempt_id=1,
            ),
        ],
    )
    return [
        (
            "binary_per_step_half",
            {
                "coverage": {"per_step": {"eq.one": "covered", "eq.two": "missing"}},
                "reference_nodes": two,
                "centrality": compute_centrality(KGGraph(nodes=two)),
            },
        ),
        (
            "partial_credit_with_span",
            {
                "coverage": {
                    "per_step": {"eq.one": "missing", "eq.two": "covered"},
                    "procedure_scores": {"eq.one": 0.4, "eq.two": 1.0},
                },
                "reference_nodes": two,
                "centrality": compute_centrality(KGGraph(nodes=two)),
                "evidence_spans": {"eq.one": "my own words"},
            },
        ),
        (
            "hoot_assisted_mixed",
            {
                "coverage": {
                    "per_step": {"eq.one": "missing", "eq.two": "covered"},
                    "procedure_scores": {"eq.one": 0.5, "eq.two": 1.0},
                    "hoot_assisted": {"eq.one": True, "eq.two": False},
                },
                "reference_nodes": two,
                "centrality": compute_centrality(KGGraph(nodes=two)),
            },
        ),
        (
            "unprobed_exclusion",
            {
                "coverage": {
                    "per_step": {"eq.one": "covered", "c.one": "missing", "p.one": "missing"},
                    "procedure_scores": {"eq.one": 1.0, "c.one": 0.0, "p.one": 0.9},
                },
                "reference_nodes": three,
                "centrality": compute_centrality(KGGraph(nodes=three)),
                "asked_node_ids": frozenset({"eq.one"}),
            },
        ),
        (
            "denominator_floor_widened",
            {
                "coverage": {
                    "per_step": {node.node_id: "missing" for node in four},
                    "procedure_scores": {"p.one": 0.5, "p.two": 0.3, "p.three": 0.2, "p.four": 0.1},
                },
                "reference_nodes": four,
                "centrality": compute_centrality(KGGraph(nodes=four)),
                "asked_node_ids": frozenset({"p.one"}),
            },
        ),
        (
            "all_covered_perfect",
            {
                "coverage": {
                    "per_step": {node.node_id: "covered" for node in three},
                    "procedure_scores": {node.node_id: 1.0 for node in three},
                },
                "reference_nodes": three,
                "centrality": compute_centrality(KGGraph(nodes=three)),
            },
        ),
        (
            "mixed_types_structural_centrality",
            {
                "coverage": {
                    "per_step": {
                        "eq.one": "covered",
                        "c.one": "missing",
                        "s.one": "missing",
                        "p.one": "missing",
                    },
                    "procedure_scores": {
                        "eq.one": 1.0,
                        "c.one": 0.55,
                        "s.one": 0.0,
                        "p.one": 0.8,
                    },
                },
                "reference_nodes": mixed,
                "centrality": compute_centrality(chain),
                "evidence_spans": {"eq.one": "pressure plus dynamic head is constant"},
            },
        ),
        (
            "empty_reference_graph",
            {"coverage": {}, "reference_nodes": [], "centrality": {}},
        ),
    ]
