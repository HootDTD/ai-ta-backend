"""Apollo §6 graph-compare package (WU-4A1) — build S_norm/R_norm + validate.

The "build half" of the §6 grading core. Standalone + pure (mirrors
``apollo/resolution/``): it turns ``(frozen student KGGraph, ResolutionResult,
problem dict)`` into two immutable canonical graphs (``CanonicalGraph`` /
``ReferenceGraph``) and validates both raw graphs first. It computes NO scores,
runs NO simulation, persists nothing, and calls neither Neo4j nor any LLM —
scores/findings (``Finding``/``grade_attempt``) are WU-4A2; the live
``resolve_attempt`` call + Done-route wiring are WU-4C.

Consumes ``apollo.resolution`` (the §5 resolver + candidate builders) and
``apollo.persistence.learner_model_seed.validate_reference_graph`` (the §6.1
reference contract, REUSED for the reference side).
"""

from __future__ import annotations

from apollo.graph_compare.canonical import (
    CanonicalEdge,
    CanonicalGraph,
    CanonicalNode,
    ReferenceGraph,
    ReferencePathView,
    build_reference_canonical,
    build_student_canonical,
)
from apollo.graph_compare.problem_inputs import (
    ProblemInputs,
    build_problem_candidates,
)
from apollo.graph_compare.validator import (
    ReferenceGraphInvalidError,
    StudentGraphInvalidError,
    validate_reference,
    validate_student_graph,
)

__all__ = [
    "CanonicalNode",
    "CanonicalEdge",
    "CanonicalGraph",
    "ReferencePathView",
    "ReferenceGraph",
    "build_student_canonical",
    "build_reference_canonical",
    "validate_student_graph",
    "validate_reference",
    "StudentGraphInvalidError",
    "ReferenceGraphInvalidError",
    "ProblemInputs",
    "build_problem_candidates",
]
