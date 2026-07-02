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


def test_parse_chained_equality_uses_first_equality():
    """WU-AAS lane B2.2 / G4.2: a chained equality (``symbol = formula =
    numeric-substitution = ...``, the mainstream physics-writeup form) is
    NORMALIZED to its FIRST equality (the symbolic statement) rather than
    REJECTED. ``a = b = c`` -> zero-form ``a - b``; the trailing terms (numeric
    substitutions / units) are discarded, so the equation the solver/gates see is
    the clean symbolic relationship."""
    from sympy import Symbol, simplify

    expr = parse_zero_form("a = b = c", entry_id="chain")
    a, b = Symbol("a"), Symbol("b")
    assert simplify(expr - (a - b)) == 0


def test_parse_chained_equality_keeps_symbolic_free_symbols():
    """The EXACT F1a-logged gate-6 reject #1: ``v = v0 + a*t = 3 + 2*5``. The first
    equality (``v = v0 + a*t``) is retained, so the free-symbol set is the SYMBOLIC
    one {v, v0, a, t} (NOT the numeric tail ``3 + 2*5``)."""
    expr = parse_zero_form("v = v0 + a*t = 3 + 2*5", entry_id="eq2")
    assert {s.name for s in expr.free_symbols} == {"v", "v0", "a", "t"}


def test_parse_caret_is_exponent():
    """WU-AAS lane B2.2 / G4.2: ``^`` is normalized to SymPy power (``**``) instead
    of being parsed as XOR (which raised ``unsupported operand type(s) for ^``)."""
    from sympy import Symbol, simplify

    expr = parse_zero_form("y = a*t^2", entry_id="pow")
    y, a, t = Symbol("y"), Symbol("a"), Symbol("t")
    assert simplify(expr - (y - a * t ** 2)) == 0


def test_parse_chained_equality_with_caret_and_units_tail():
    """The EXACT F1a-logged gate-6 reject #2: ``x = v0*t + (1/2)*a*t^2 = 0 +
    0.5*(2.0)*(5.0)^2 = 25.0 m``. Chained equality + ``^`` + a unit-bearing numeric
    tail (`25.0 m`) — all three tolerated: normalized to the first equality (which
    itself carries a ``^``), the unit tail discarded. Free symbols are the symbolic
    {x, v0, a, t}."""
    expr = parse_zero_form(
        "x = v0*t + (1/2)*a*t^2 = 0 + 0.5*(2.0)*(5.0)^2 = 25.0 m", entry_id="eq2"
    )
    assert {s.name for s in expr.free_symbols} == {"x", "v0", "a", "t"}


def test_parse_still_raises_on_unbalanced_parens():
    """Counter-test: a genuinely malformed equation (unbalanced parens) is STILL
    rejected — the tolerance loosening does not neuter the gate."""
    with pytest.raises(MalformedEquationError):
        parse_zero_form("v = v0 + (a*t", entry_id="junk")


def test_parse_still_raises_on_empty_rhs():
    """Counter-test: an empty RHS (``v =``) is STILL rejected."""
    with pytest.raises(MalformedEquationError):
        parse_zero_form("v = ", entry_id="junk")


def test_parse_still_raises_on_empty_middle_of_chain():
    """Counter-test: a chained equality with an EMPTY first-equality operand
    (``v = = 3``) is STILL rejected — normalization to the first equality does not
    paper over a malformed chain."""
    with pytest.raises(MalformedEquationError):
        parse_zero_form("v = = 3", entry_id="junk")


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
