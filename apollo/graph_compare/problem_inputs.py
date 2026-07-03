"""WU-4A1 — per-problem resolver inputs assembly (candidates + symbolic_mappings).

The seam where the closed candidate set AND the per-problem ``symbolic_mappings``
table are assembled to feed ``resolve_attempt``. WU-4A1 does NOT call the
resolver itself — the caller (WU-4C) does
``resolve_attempt(student_graph, inputs.candidates, symbolic_mappings=inputs.symbolic_mappings)``.
This module only builds the inputs, REUSING the WU-3C2 candidate builders rather
than reimplementing them.

``symbolic_mappings`` is PER-PROBLEM declared data (§5, Decision 2): the
problem-level ``symbolic_mappings`` key PLUS every declared ``simplification``'s
explicit ``content.substitution`` map (so a student equation stated in the
*derived*, post-simplification form resolves to the governing equation). It is
returned as a NEW dict, never an alias into the problem (immutability).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from apollo.resolution import (
    Candidate,
    build_candidate_set,
    candidates_from_misconceptions,
    candidates_from_reference_solution,
)


@dataclass(frozen=True)
class ProblemInputs:
    """The assembled resolver inputs for one problem. Immutable.

    ``candidates`` is the closed candidate set (this problem's reference nodes +
    the course misconceptions); ``symbolic_mappings`` is the per-problem declared
    variable-substitution table the resolver's symbolic tier applies (``{}`` when
    the problem declares none)."""

    candidates: tuple[Candidate, ...]
    symbolic_mappings: dict[str, str] = field(default_factory=dict)
    # A1-iter2 — the problem's declared numeric knowns (``given_values``),
    # ALREADY known to the graph-sim chain (solver/forward_chain.py,
    # solver/sufficiency.py read the same problem key). Threaded through so
    # the default-OFF equivalence tier's numeric-instantiation check can use
    # DECLARED data only — never invented. Every OTHER tier ignores this
    # field entirely (unchanged behavior when the flag is off).
    given_values: dict[str, str] = field(default_factory=dict)


def build_problem_candidates(
    problem: dict,
    misconceptions: dict,
    *,
    canon_key_by_canonical_key: dict[str, int],
) -> ProblemInputs:
    """Assemble the closed candidate set + read the per-problem symbolic mappings.

    REUSES ``candidates_from_reference_solution`` /
    ``candidates_from_misconceptions`` / ``build_candidate_set`` (WU-3C2). The
    ``symbolic_mappings`` is a NEW dict copied from the problem's
    ``symbolic_mappings`` key (default ``{}`` — Decision 2)."""
    refs = candidates_from_reference_solution(
        problem, canon_key_by_canonical_key=canon_key_by_canonical_key
    )
    miscs = candidates_from_misconceptions(
        misconceptions, canon_key_by_canonical_key=canon_key_by_canonical_key
    )
    candidates = build_candidate_set(reference_nodes=refs, misconception_entities=miscs)
    symbolic_mappings = _collect_symbolic_mappings(problem)
    given_values = _collect_given_values(problem)
    return ProblemInputs(
        candidates=candidates, symbolic_mappings=symbolic_mappings, given_values=given_values
    )


def _collect_symbolic_mappings(problem: dict) -> dict[str, str]:
    """Assemble the resolver's per-problem symbolic substitution table.

    Base = the problem-level ``symbolic_mappings`` key (default ``{}``). Then add
    every declared ``simplification``'s explicit ``content.substitution`` map —
    the deterministic, subject-agnostic source for collapsing a derived equation
    form onto its governing entity. ``applies_when`` is human-facing (a symbolic
    relation OR a natural-language concept) and is NEVER parsed.

    A simplification with no ``substitution`` (a purely conceptual precondition)
    contributes nothing. The explicit problem-level table WINS on a key collision
    (``setdefault``), preserving its authority. Returns a NEW dict (never an alias
    into the problem); keys/values are coerced to ``str`` for the symbolic tier.
    """
    mappings: dict[str, str] = {
        str(k): str(v) for k, v in (problem.get("symbolic_mappings", {}) or {}).items()
    }
    for step in problem.get("reference_solution", []):
        if step.get("entry_type") != "simplification":
            continue
        substitution = (step.get("content", {}) or {}).get("substitution") or {}
        for var, expr in substitution.items():
            mappings.setdefault(str(var), str(expr))
    return mappings


def _collect_given_values(problem: dict) -> dict[str, str]:
    """The problem's declared numeric knowns (A1-iter2), coerced to
    ``str`` for the same SymPy parsing path the symbolic tiers already use.
    Returns a NEW dict (never an alias into the problem). Default ``{}`` when
    the problem declares none — every existing tier is unaffected either way;
    only the default-OFF equivalence tier reads this field."""
    return {str(k): str(v) for k, v in (problem.get("given_values", {}) or {}).items()}
