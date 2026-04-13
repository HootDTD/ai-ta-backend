"""Throwaway SymPy solver for Bernoulli problem_01.

Hardcoded to problem 01 (horizontal pipe, find P2). This exists only
to let the Week 2 spike run end-to-end. Real solver is built Week 3+.
"""
from __future__ import annotations

from typing import Any, Dict, List

from sympy import Rational, Symbol, parse_expr, solve, symbols  # noqa: F401


PROBLEM_01_GIVENS = {
    "rho": 1000.0,
    "A1": 0.01,
    "P1": 200000.0,
    "v1": 2.0,
    "A2": 0.005,
    "h1": 0.0,
    "h2": 0.0,
    "g": 9.81,
}

PROBLEM_01_TARGET = "P2"


def _all_symbols_from_exprs(exprs: List[Any]) -> set[str]:
    out: set[str] = set()
    for e in exprs:
        for s in e.free_symbols:
            out.add(s.name)
    return out


def solve_problem_01(kg: Dict[str, Any]) -> Dict[str, Any]:
    """Solve Bernoulli problem 01 given a KG dict.

    kg has keys 'equations' (list[str]) and 'conditions' (list[str]).
    Returns {success, value?, missing?, error?}.
    """
    from sympy.parsing.sympy_parser import parse_expr

    local = {name: Symbol(name) for name in [
        "rho", "A1", "A2", "v1", "v2", "P1", "P2", "g", "h1", "h2"
    ]}
    local["Rational"] = Rational

    try:
        parsed_eqs = [parse_expr(e, local_dict=local) for e in kg.get("equations", [])]
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"parse error: {exc}"}

    target = Symbol(PROBLEM_01_TARGET)
    substituted = []
    for e in parsed_eqs:
        s = e
        for name, value in PROBLEM_01_GIVENS.items():
            s = s.subs(Symbol(name), value)
        substituted.append(s)

    # The horizontal simplification is enforced by givens (h1=h2=0).
    # Try to solve for P2; introduce v2 as a free unknown to be pinned
    # by continuity if present.
    unknowns = _all_symbols_from_exprs(substituted) - set(PROBLEM_01_GIVENS.keys())
    if target.name not in unknowns and target.name not in _all_symbols_from_exprs(parsed_eqs):
        return {"success": False, "error": f"{PROBLEM_01_TARGET} not present in KG equations"}

    sols = solve(substituted, list({Symbol(u) for u in unknowns}), dict=True)
    if not sols:
        missing = sorted(unknowns - {PROBLEM_01_TARGET})
        return {"success": False, "missing": missing}

    # Pick the first fully-determined real solution with P2 present.
    for sol in sols:
        if target in sol:
            val = sol[target]
            if val.is_real:
                return {"success": True, "value": val}
            # val still has free symbols — we have an underdetermined system.
            # Report whatever unknowns remain in the expression as missing.
            still_free = sorted(s.name for s in val.free_symbols)
            return {"success": False, "missing": still_free}
    return {"success": False, "error": "no real solution for P2"}
