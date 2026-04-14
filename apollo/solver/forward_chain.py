"""Forward-chaining planner: KG equations + problem givens → solve for target.

No LLM. The planner:
  1. Parses each KG equation into zero-form SymPy (raises MalformedEquationError on failure).
  2. Calls solve_system with (equations, givens, target) from the problem.
  3. Returns the result dict directly (success or stuck with missing variables).
"""
from __future__ import annotations

from typing import Any, Dict

from apollo.solver.sympy_exec import parse_zero_form, solve_system


def solve_kg_against_problem(kg: Dict[str, Any], problem: Dict[str, Any]) -> Dict[str, Any]:
    equations = []
    for idx, entry in enumerate(kg.get("equation", [])):
        symbolic = entry.get("symbolic", "")
        label = entry.get("label") or f"equation_{idx}"
        parsed = parse_zero_form(symbolic, entry_id=label)
        equations.append(parsed)

    if not equations:
        return {
            "status": "stuck",
            "missing_variables": [problem["target_unknown"]],
            "trace": [{"op": "empty_kg"}],
        }

    return solve_system(equations, problem["given_values"], problem["target_unknown"])
