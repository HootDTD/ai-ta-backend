"""SymPy wrapper: parse zero-form expressions and solve systems.

Raises MalformedEquationError attributed to a specific KG entry when
parsing fails. NEVER silently skips entries — all-or-nothing parse.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from sympy import Float, Integer, Rational, Symbol, latex, simplify, solve
from sympy.parsing.sympy_parser import (
    convert_xor,
    parse_expr,
    standard_transformations,
)

from apollo.errors import MalformedEquationError

_LOG = logging.getLogger(__name__)

# ``convert_xor`` normalizes ``^`` (which SymPy otherwise parses as bitwise XOR,
# raising ``unsupported operand type(s) for ^``) to ``Pow`` — the mainstream
# textbook / worked-solution exponent notation a real teacher PDF carries. Added
# to the standard set so every ``parse_expr`` below treats ``^`` as exponentiation.
_TRANSFORMATIONS = standard_transformations + (convert_xor,)


def _tidy_floats(expr):
    """Collapse whole-number SymPy Floats (e.g., 500.0) to Integer for cleaner display."""
    try:
        return expr.replace(
            lambda e: isinstance(e, Float) and e == int(e),
            lambda e: Integer(int(e)),
        )
    except Exception:  # noqa: BLE001
        return expr


def _format_value_text(val) -> str:
    """Human-friendly rendering for a solver value: drops SymPy Float trailing zeros."""
    try:
        f = float(val)
        return f"{f:.6g}"
    except (TypeError, ValueError):
        return str(val)

_CANONICAL_SYMBOLS = [
    "rho", "A", "A1", "A2", "P", "P1", "P2",
    "v", "v1", "v2", "g", "h", "h1", "h2", "Q", "q",
]


def _local_dict() -> Dict[str, Any]:
    d: Dict[str, Any] = {name: Symbol(name) for name in _CANONICAL_SYMBOLS}
    d["Rational"] = Rational
    return d


def parse_zero_form(
    symbolic: str,
    *,
    entry_id: str,
    local_dict: Dict[str, Any] | None = None,
):
    """Parse a student-taught equation in 'LHS = RHS', a CHAINED equality
    ('A = B = C = ...') or 'LHS - (RHS)' form and return a SymPy expression
    representing LHS - RHS.

    TOLERANCES (WU-AAS lane B2.2 / G4.2 — real teacher-PDF notations):
      * CHAINED equality — a physics worked-solution routinely writes
        ``symbol = symbolic-formula = numeric-substitution = final-value`` on one
        line. We normalize to the FIRST equality (``A = B``): that is the symbolic
        statement the gates and solver need; the trailing numeric-substitution /
        unit-bearing tail (e.g. ``= 25.0 m``) — and any further ``= C = D``
        segments — are DISCARDED (debug-logged, never raised). Dropping the tail
        can only UNDER-constrain (the safe direction — gate 7 catches an
        under-determined system), never admit a wrong equation.
      * ``^`` exponent — normalized to ``Pow`` via ``convert_xor`` (see
        ``_TRANSFORMATIONS``) instead of raising SymPy's XOR type error.

    ``local_dict`` overrides the canonical symbol table (defaults to
    ``_local_dict()``). This is the SINGLE mint-time parser: the resolution tier
    (``apollo.resolution.tiers._zero_form``) delegates here with its own extended
    locals so a minted subject and the runtime grader parse identical notations —
    there is no second, divergent equation parser (WU-AAS lane B2.2 Finding 1).
    """
    ld = _local_dict() if local_dict is None else local_dict
    s = symbolic.strip()
    if "=" in s:
        parts = s.split("=")
        # Chained equality (A = B = C = ...): keep the FIRST equality (A = B), the
        # symbolic relationship. A single '=' is the len==2 (pairwise) case,
        # unchanged. len==1 is impossible here (we only enter on a '=' present).
        lhs, rhs = parts[0], parts[1]
        if len(parts) > 2:
            _LOG.debug(
                "sympy_parse_chained_equality_normalized",
                extra={
                    "event": "sympy_parse_chained_equality_normalized",
                    "entry_id": entry_id,
                    "equality_count": len(parts) - 1,
                },
            )
        s = f"({lhs.strip()}) - ({rhs.strip()})"

    try:
        return parse_expr(s, local_dict=ld, transformations=_TRANSFORMATIONS)
    except Exception as exc:  # noqa: BLE001
        raise MalformedEquationError(
            entry_id=entry_id,
            symbolic=symbolic,
            parse_error=str(exc),
        ) from exc


def solve_system(equations: List[Any], givens: Dict[str, float], target: str) -> Dict[str, Any]:
    """Solve the simultaneous system."""
    trace: List[Dict[str, Any]] = []
    target_sym = Symbol(target)

    substituted = []
    for eq in equations:
        cur = eq
        for name, value in givens.items():
            cur = cur.subs(Symbol(name), value)
        substituted.append(cur)
        tidy = _tidy_floats(cur)
        trace.append({
            "op": "substitute_givens",
            "expr": str(tidy),
            "expr_latex": latex(tidy),
        })

    unknowns = set()
    for eq in substituted:
        for sym in eq.free_symbols:
            if sym.name not in givens:
                unknowns.add(sym)

    if target_sym not in unknowns and target_sym not in {s for eq in equations for s in eq.free_symbols}:
        return {
            "status": "stuck",
            "missing_variables": [target],
            "trace": trace + [{"op": "target_absent", "target": target}],
        }

    sols = solve(substituted, list(unknowns), dict=True)
    trace.append({"op": "solve_system", "num_solutions": len(sols)})

    for sol in sols:
        if target_sym in sol:
            val = sol[target_sym]
            if val.is_real is True:
                trace.append({
                    "op": "pick_real_solution",
                    "target": target,
                    "value": _format_value_text(val),
                    "value_latex": latex(_tidy_floats(val)),
                })
                return {"status": "solved", "value": val, "trace": trace}
            remaining = sorted(s.name for s in val.free_symbols if s.name not in givens)
            if remaining:
                tidy = _tidy_floats(val)
                return {
                    "status": "stuck",
                    "missing_variables": remaining,
                    "trace": trace + [{
                        "op": "parameterized_solution",
                        "expression": str(tidy),
                        "expression_latex": latex(tidy),
                    }],
                }

    missing = sorted(s.name for s in unknowns if s.name != target)
    return {
        "status": "stuck",
        "missing_variables": missing,
        "trace": trace + [{"op": "no_real_solution"}],
    }
