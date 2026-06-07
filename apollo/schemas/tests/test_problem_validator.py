"""V3 reference-side validator tests (checklist item 7)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from apollo.schemas.problem import Problem, load_problem

# Bundled Bernoulli problem bank on disk. load_concept is now Neo4j-backed and no
# longer exposes a filesystem problems_dir, so this test reads the bank directly.
# The bank moves into Neo4j in the bernoulli migration (task A10).
_BERNOULLI_PROBLEMS_DIR = (
    Path(__file__).resolve().parents[2]
    / "subjects" / "fluid_mechanics" / "concepts" / "bernoulli_principle" / "problems"
)


def _base_problem() -> dict:
    return {
        "id": "test_problem",
        "concept_id": "bernoulli_principle",
        "difficulty": "intro",
        "problem_text": "x",
        "given_values": {"x": 1.0},
        "target_unknown": "y",
        "reference_solution": [
            {
                "step": 1, "entry_type": "equation", "id": "eq1",
                "content": {"symbolic": "x - y", "label": "eq1"},
                "depends_on": [],
            },
        ],
    }


def test_validator_rejects_unresolved_depends_on():
    payload = _base_problem()
    payload["reference_solution"].append({
        "step": 2, "entry_type": "condition", "id": "c1",
        "content": {"applies_when": "x", "label": "c1"},
        "depends_on": ["NONEXISTENT"],
    })
    with pytest.raises(ValidationError, match="depends_on 'NONEXISTENT'"):
        Problem.model_validate(payload)


def test_validator_rejects_uses_equations_typo():
    payload = _base_problem()
    payload["reference_solution"].append({
        "step": 2, "entry_type": "procedure_step", "id": "p1",
        "content": {"order": 1, "action": "do", "purpose": "p",
                    "uses_equations": ["eq1_TYPO"]},
        "depends_on": [],
    })
    with pytest.raises(ValidationError, match="uses_equations 'eq1_TYPO'"):
        Problem.model_validate(payload)


def test_validator_rejects_non_contiguous_order():
    payload = _base_problem()
    payload["reference_solution"].extend([
        {"step": 2, "entry_type": "procedure_step", "id": "p1",
         "content": {"order": 1, "action": "a", "purpose": "p", "uses_equations": []},
         "depends_on": []},
        {"step": 3, "entry_type": "procedure_step", "id": "p2",
         "content": {"order": 5, "action": "b", "purpose": "p", "uses_equations": []},
         "depends_on": []},
    ])
    with pytest.raises(ValidationError, match="order=5"):
        Problem.model_validate(payload)


def test_validator_accepts_clean_problem():
    payload = _base_problem()
    payload["reference_solution"].append({
        "step": 2, "entry_type": "procedure_step", "id": "p1",
        "content": {"order": 1, "action": "do", "purpose": "p",
                    "uses_equations": ["eq1"]},
        "depends_on": ["eq1"],
    })
    p = Problem.model_validate(payload)
    assert p.id == "test_problem"


def test_all_bundled_bernoulli_problems_validate():
    """Every bundled Bernoulli problem JSON must pass the V3 validator."""
    bundled = sorted(_BERNOULLI_PROBLEMS_DIR.glob("problem_*.json"))
    assert len(bundled) >= 1, "expected bundled problems"
    for path in bundled:
        problem = load_problem(path)
        # to_kg_graph derives nodes/edges; raises if anything is broken
        graph = problem.to_kg_graph(attempt_id=-1)
        assert len(graph.nodes) >= 1
