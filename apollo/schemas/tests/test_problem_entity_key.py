"""entity_key survives Problem validation and reaches the reference graph."""

from __future__ import annotations

import pytest

from apollo.schemas.problem import Problem

pytestmark = pytest.mark.unit

_PROBLEM = {
    "id": "p1",
    "concept_id": "nominal_vs_real_gdp",
    "difficulty": "hard",
    "problem_text": "compute real growth",
    "reference_solution": [
        {
            "step": 1,
            "entry_type": "definition",
            "id": "real_basis",
            "content": {"concept": "real GDP", "meaning": "inflation-adjusted"},
            "entity_key": "def.real_basis",
        },
        {
            "step": 2,
            "entry_type": "procedure_step",
            "id": "do_it",
            "content": {"action": "subtract", "purpose": "isolate", "order": 1},
            "depends_on": ["real_basis"],
            "entity_key": "proc.do_it",
        },
    ],
}


def test_reference_step_keeps_entity_key() -> None:
    prob = Problem.model_validate(_PROBLEM)
    step = next(s for s in prob.reference_solution if s.id == "real_basis")
    assert step.entity_key == "def.real_basis"


def test_to_kg_graph_reference_nodes_carry_entity_key() -> None:
    prob = Problem.model_validate(_PROBLEM)
    graph = prob.to_kg_graph(attempt_id=7)
    by_id = {n.node_id: n for n in graph.nodes}
    assert by_id["real_basis"].entity_key == "def.real_basis"
    assert by_id["do_it"].entity_key == "proc.do_it"


def test_to_kg_graph_depends_on_is_prerequisite_then_dependent() -> None:
    graph = Problem.model_validate(_PROBLEM).to_kg_graph(attempt_id=7)
    dependency = next(edge for edge in graph.edges if edge.edge_type.value == "DEPENDS_ON")
    assert (dependency.from_node_id, dependency.to_node_id) == ("real_basis", "do_it")


def test_missing_entity_key_is_derived() -> None:
    """T1: a step that omits entity_key now gets it derived at graph-build time
    (population case) instead of defaulting to None."""
    payload = {
        "id": "p2",
        "concept_id": "c",
        "difficulty": "hard",
        "problem_text": "x",
        "reference_solution": [
            {
                "step": 1,
                "entry_type": "equation",
                "id": "e",
                "content": {"symbolic": "a-b"},
            }
        ],
    }
    graph = Problem.model_validate(payload).to_kg_graph(attempt_id=1)
    assert graph.nodes[0].entity_key == "eq.e"


def test_authored_entity_key_is_never_rederived() -> None:
    """Byte-identity: a step that already carries entity_key keeps exactly that
    value even if it does not match what derivation would produce."""
    payload = {
        "id": "p3",
        "concept_id": "c",
        "difficulty": "hard",
        "problem_text": "x",
        "reference_solution": [
            {
                "step": 1,
                "entry_type": "equation",
                "id": "e",
                "content": {"symbolic": "a-b"},
                "entity_key": "eq.custom_authored_value",
            }
        ],
    }
    graph = Problem.model_validate(payload).to_kg_graph(attempt_id=1)
    assert graph.nodes[0].entity_key == "eq.custom_authored_value"
