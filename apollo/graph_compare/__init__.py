"""Apollo §6 graph-compare package — the grading core (WU-4A1 + WU-4A2).

The "build half" (WU-4A1) is standalone + pure (mirrors ``apollo/resolution/``):
it turns ``(frozen student KGGraph, ResolutionResult, problem dict)`` into two
immutable canonical graphs (``CanonicalGraph`` / ``ReferenceGraph``) and
validates both raw graphs first.

The "compare half" (WU-4A2) is the deterministic score-math: ``grade_attempt``
takes the two canonical graphs and returns a frozen ``GradeResult`` (3 top-line
scores + 7 sub-scores + in-memory ``Finding``s + ``comparison_version``). It
computes coverage (MAX over declared paths), soundness (contradictions-only via
the ``misc.`` key prefix), bisimilarity (harmonic mean; ``a+b==0 -> 0``, never
NaN), and emits the §2 finding set. It still persists NOTHING, runs NO Neo4j /
Postgres / LLM, and emits NO events — finding->event conversion, abstention, and
the runs/findings persistence are WU-4B; the live ``resolve_attempt`` call +
Done-route wiring are WU-4C.

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
from apollo.graph_compare.core import (
    COMPARISON_VERSION,
    GradeResult,
    grade_attempt,
)
from apollo.graph_compare.findings import (
    Finding,
    FindingKind,
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
    # WU-4A2 — the §6 grading-core COMPARE half (scores + findings + grade_attempt).
    "grade_attempt",
    "GradeResult",
    "COMPARISON_VERSION",
    "Finding",
    "FindingKind",
]
