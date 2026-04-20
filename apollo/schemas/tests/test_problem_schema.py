import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from apollo.schemas.problem import Problem, load_problem


def _minimal_problem_dict():
    return {
        "id": "p1",
        "concept_id": "bernoulli",
        "difficulty": "intro",
        "problem_text": "A pipe...",
        "given_values": {"P1": 200000.0, "rho": 1000.0},
        "target_unknown": "P2",
        "reference_solution": [
            {
                "step": 1,
                "entry_type": "equation",
                "id": "bernoulli_eq",
                "content": {"symbolic": "P1 + 0.5*rho*v1**2 = P2 + 0.5*rho*v2**2"},
                "depends_on": [],
            }
        ],
    }


def test_problem_accepts_valid_minimal():
    p = Problem.model_validate(_minimal_problem_dict())
    assert p.target_unknown == "P2"
    assert p.reference_solution[0].entry_type == "equation"


def test_problem_rejects_empty_reference_solution():
    data = _minimal_problem_dict()
    data["reference_solution"] = []
    with pytest.raises(ValidationError):
        Problem.model_validate(data)


def test_problem_rejects_invalid_entry_type():
    data = _minimal_problem_dict()
    data["reference_solution"][0]["entry_type"] = "theorem"
    with pytest.raises(ValidationError):
        Problem.model_validate(data)


def test_problem_rejects_invalid_difficulty():
    data = _minimal_problem_dict()
    data["difficulty"] = "insane"
    with pytest.raises(ValidationError):
        Problem.model_validate(data)


def test_load_problem_reads_file(tmp_path: Path):
    p = tmp_path / "prob.json"
    p.write_text(json.dumps(_minimal_problem_dict()))
    prob = load_problem(p)
    assert prob.id == "p1"


def test_problem_accepts_procedure_step_in_reference_solution():
    p = Problem.model_validate({
        "id": "demo",
        "concept_id": "demo_concept",
        "difficulty": "intro",
        "problem_text": "demo",
        "given_values": {"x": 1.0},
        "target_unknown": "y",
        "reference_solution": [
            {
                "step": 1,
                "entry_type": "procedure_step",
                "id": "plan_step_1",
                "content": {
                    "order": 1,
                    "action": "do x",
                    "uses_equations": ["eq1"],
                    "purpose": "find y",
                },
                "depends_on": [],
            }
        ],
    })
    assert p.reference_solution[0].entry_type == "procedure_step"


def test_all_bernoulli_problems_have_procedure_steps():
    from pathlib import Path
    from apollo.schemas.problem import load_problem

    bernoulli_dir = Path(__file__).resolve().parents[3] / "apollo" / "problems" / "bernoulli"
    problems = sorted(bernoulli_dir.glob("problem_*.json"))
    assert len(problems) == 5, f"expected 5 problems, found {len(problems)}"
    for path in problems:
        p = load_problem(path)
        step_types = [s.entry_type for s in p.reference_solution]
        assert "procedure_step" in step_types, (
            f"{path.name} has no procedure_step entries in reference_solution"
        )
        procedure_steps = [s for s in p.reference_solution if s.entry_type == "procedure_step"]
        orders = sorted(s.content["order"] for s in procedure_steps)
        assert orders == list(range(1, len(orders) + 1)), (
            f"{path.name} procedure_step orders are not a contiguous 1..N: {orders}"
        )
