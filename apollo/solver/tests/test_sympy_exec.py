import pytest

from apollo.errors import MalformedEquationError
from apollo.solver.sympy_exec import parse_zero_form, solve_system


def test_parse_zero_form_converts_lhs_equals_rhs():
    expr = parse_zero_form("A1*v1 = A2*v2", entry_id="continuity")
    from sympy import Symbol, simplify
    A1, v1, A2, v2 = Symbol("A1"), Symbol("v1"), Symbol("A2"), Symbol("v2")
    assert simplify(expr - (A1 * v1 - A2 * v2)) == 0


def test_parse_zero_form_accepts_already_zero_form():
    expr = parse_zero_form("A1*v1 - A2*v2", entry_id="continuity")
    from sympy import Symbol, simplify
    A1, v1, A2, v2 = Symbol("A1"), Symbol("v1"), Symbol("A2"), Symbol("v2")
    assert simplify(expr - (A1 * v1 - A2 * v2)) == 0


def test_parse_raises_on_malformed():
    with pytest.raises(MalformedEquationError) as exc_info:
        parse_zero_form("@@@ not an equation @@@", entry_id="junk")
    assert exc_info.value.entry_id == "junk"


def test_parse_raises_on_multiple_equals():
    with pytest.raises(MalformedEquationError):
        parse_zero_form("a = b = c", entry_id="chain")


def test_solve_system_bernoulli_horizontal():
    equations = [
        parse_zero_form("rho*A1*v1 - rho*A2*v2", entry_id="continuity"),
        parse_zero_form(
            "P1 + Rational(1,2)*rho*v1**2 + rho*g*h1 - (P2 + Rational(1,2)*rho*v2**2 + rho*g*h2)",
            entry_id="bernoulli",
        ),
    ]
    givens = {"rho": 1000, "A1": 0.01, "P1": 200000, "v1": 2.0, "A2": 0.005, "h1": 0, "h2": 0, "g": 9.81}
    target = "P2"

    result = solve_system(equations, givens, target)
    assert result["status"] == "solved"
    assert abs(float(result["value"]) - 194000.0) < 1e-3


def test_solve_system_stuck_when_missing_equation():
    equations = [
        parse_zero_form(
            "P1 + Rational(1,2)*rho*v1**2 + rho*g*h1 - (P2 + Rational(1,2)*rho*v2**2 + rho*g*h2)",
            entry_id="bernoulli",
        ),
    ]
    givens = {"rho": 1000, "A1": 0.01, "P1": 200000, "v1": 2.0, "A2": 0.005, "h1": 0, "h2": 0, "g": 9.81}
    target = "P2"

    result = solve_system(equations, givens, target)
    assert result["status"] == "stuck"
    assert "v2" in result["missing_variables"]
