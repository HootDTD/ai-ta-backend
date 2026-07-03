import pytest

from apollo.errors import MalformedEquationError
from apollo.solver.forward_chain import solve_kg_against_problem


def _kg(equations):
    return {
        "equation": [{"symbolic": s, "label": lab} for (s, lab) in equations],
        "definition": [],
        "condition": [],
        "simplification": [],
        "variable_mapping": [],
    }


PROBLEM_01 = {
    "id": "bernoulli_horizontal_pipe_find_p2",
    "given_values": {"rho": 1000, "A1": 0.01, "P1": 200000, "v1": 2.0, "A2": 0.005, "h1": 0, "h2": 0, "g": 9.81},
    "target_unknown": "P2",
}


def test_solve_with_complete_kg_produces_correct_p2():
    kg = _kg([
        ("rho*A1*v1 - rho*A2*v2", "Continuity"),
        ("P1 + Rational(1,2)*rho*v1**2 + rho*g*h1 - (P2 + Rational(1,2)*rho*v2**2 + rho*g*h2)", "Bernoulli"),
    ])
    result = solve_kg_against_problem(kg, PROBLEM_01)
    assert result["status"] == "solved"
    assert abs(float(result["value"]) - 194000.0) < 1e-3


def test_solve_with_missing_continuity_is_stuck_with_v2_missing():
    kg = _kg([
        ("P1 + Rational(1,2)*rho*v1**2 + rho*g*h1 - (P2 + Rational(1,2)*rho*v2**2 + rho*g*h2)", "Bernoulli"),
    ])
    result = solve_kg_against_problem(kg, PROBLEM_01)
    assert result["status"] == "stuck"
    assert "v2" in result["missing_variables"]


def test_solve_malformed_equation_raises():
    kg = _kg([("@@ broken @@", "Garbage")])
    with pytest.raises(MalformedEquationError):
        solve_kg_against_problem(kg, PROBLEM_01)


def test_empty_kg_is_stuck_with_target_in_missing():
    kg = _kg([])
    result = solve_kg_against_problem(kg, PROBLEM_01)
    assert result["status"] == "stuck"
    assert "P2" in result["missing_variables"]


def test_seeded_solve_is_byte_unchanged_under_tolerant_parser():
    """WU-AAS lane B2.2 (minor a): the tolerant parser (``convert_xor`` +
    chained-equality normalization) must NOT perturb a seeded solve that carries
    NEITHER a ``^`` NOR a chained equality. The full bernoulli forward-chain still
    lands on EXACTLY 194000.0 Pa — pinned byte-exact (not just within tolerance) so
    a future parser change that silently shifts a seeded value is caught."""
    kg = _kg([
        ("rho*A1*v1 - rho*A2*v2", "Continuity"),
        ("P1 + Rational(1,2)*rho*v1**2 + rho*g*h1 - (P2 + Rational(1,2)*rho*v2**2 + rho*g*h2)", "Bernoulli"),
    ])
    result = solve_kg_against_problem(kg, PROBLEM_01)
    assert result["status"] == "solved"
    assert float(result["value"]) == 194000.0
