"""Template-based natural-language rendering of the solver trace. No LLM."""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def _line_for(entry: Dict[str, Any]) -> Optional[str]:
    op = entry.get("op")
    if op == "substitute_givens":
        return f"I substituted what I knew: {entry.get('expr', '')}."
    if op == "solve_system":
        return f"I then solved the system ({entry.get('num_solutions', 0)} candidate solutions)."
    if op == "pick_real_solution":
        return f"I picked the real solution: {entry.get('target')} = {entry.get('value')}."
    if op == "parameterized_solution":
        return (
            f"The best I got was a solution in terms of other unknowns: "
            f"{entry.get('expression', '')}."
        )
    if op == "target_absent":
        return (
            f"I looked for {entry.get('target')} in what you taught me but didn't find it "
            "anywhere."
        )
    if op == "empty_kg":
        return "You've taught me nothing yet, so I couldn't try to solve anything."
    if op == "no_real_solution":
        return "I couldn't find a real numerical solution."
    return None


def narrate_trace(
    trace: List[Dict[str, Any]],
    *,
    status: str,
    target: str,
    missing_variables: Optional[List[str]] = None,
) -> str:
    lines: List[str] = []
    for e in trace:
        line = _line_for(e)
        if line:
            lines.append(line)

    if status == "solved":
        lines.append(f"I got a value for {target}.")
    else:
        missing = missing_variables or []
        if missing:
            pretty = ", ".join(missing)
            lines.append(
                f"I got stuck because I couldn't determine {pretty} from what you taught me."
            )
        else:
            lines.append(f"I got stuck trying to find {target}.")

    return "\n".join(lines)
