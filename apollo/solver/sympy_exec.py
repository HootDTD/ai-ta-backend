"""SymPy wrapper: parse zero-form expressions and solve systems.

Raises MalformedEquationError attributed to a specific KG entry when
parsing fails. NEVER silently skips entries — all-or-nothing parse.
"""
from __future__ import annotations

from typing import Any, Dict, List

from sympy import Rational, Symbol, simplify, solve
from sympy.parsing.sympy_parser import parse_expr

from apollo.errors import MalformedEquationError

_CANONICAL_SYMBOLS = [
    "rho", "A", "A1", "A2", "P", "P1", "P2",
    "v", "v1", "v2", "g", "h", "h1", "h2", "Q", "q",
]


def _local_dict() -> Dict[str, Any]:
    d: Dict[str, Any] = {name: Symbol(name) for name in _CANONICAL_SYMBOLS}
    d["Rational"] = Rational
    return d


def parse_zero_form(symbolic: str, *, entry_id: str):
    """Parse a student-taught equation in either 'LHS = RHS' or 'LHS - (RHS)'
    form and return a SymPy expression representing LHS - RHS.
    """
    s = symbolic.strip()
    if "=" in s:
        parts = s.split("=")
        if len(parts) != 2:
            raise MalformedEquationError(
                entry_id=entry_id,
                symbolic=symbolic,
                parse_error=f"expected exactly one '=' but found {len(parts) - 1}",
            )
        lhs, rhs = parts
        s = f"({lhs.strip()}) - ({rhs.strip()})"

    try:
        return parse_expr(s, local_dict=_local_dict())
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
        trace.append({"op": "substitute_givens", "expr": str(cur)})

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
                trace.append({"op": "pick_real_solution", "target": target, "value": str(val)})
                return {"status": "solved", "value": val, "trace": trace}
            remaining = sorted(s.name for s in val.free_symbols if s.name not in givens)
            if remaining:
                return {
                    "status": "stuck",
                    "missing_variables": remaining,
                    "trace": trace + [{"op": "parameterized_solution", "expression": str(val)}],
                }

    missing = sorted(s.name for s in unknowns if s.name != target)
    return {
        "status": "stuck",
        "missing_variables": missing,
        "trace": trace + [{"op": "no_real_solution"}],
    }
