"""WU-4A1 — per-problem resolver inputs assembly (candidates + symbolic_mappings).

The seam where the closed candidate set AND the per-problem ``symbolic_mappings``
table are assembled to feed ``resolve_attempt``. WU-4A1 does NOT call the
resolver itself — the caller (WU-4C) does
``resolve_attempt(student_graph, inputs.candidates, symbolic_mappings=inputs.symbolic_mappings)``.
This module only builds the inputs, REUSING the WU-3C2 candidate builders rather
than reimplementing them.

``symbolic_mappings`` is PER-PROBLEM declared data (§5, Decision 2): read from
the problem's ``symbolic_mappings`` key, defaulting to ``{}`` when absent — and
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
    symbolic_mappings = dict(problem.get("symbolic_mappings", {}))
    return ProblemInputs(candidates=candidates, symbolic_mappings=symbolic_mappings)
