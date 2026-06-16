"""Apollo §5 reference-anchored resolver package (WU-3C2).

Standalone by design so WU-4A's ``apollo/graph_compare/`` imports it rather than
owning it (``from apollo.resolution import resolve_attempt, ...``). The resolver
maps a student's per-attempt evidence nodes onto this problem's reference nodes
(+ course misconception entities) with content-first tiers, structural
corroboration, misconception competition, bounded global assignment, and one LLM
adjudication; it returns a :class:`ResolutionResult` and never grades, simulates,
or persists (persistence lives in ``apollo.knowledge_graph.resolution_store``).
"""

from __future__ import annotations

from apollo.resolution.candidates import (
    METHOD_CONFIDENCE_CAP,
    RESOLUTION_METHODS,
    Candidate,
    build_candidate_set,
    candidates_from_misconceptions,
    candidates_from_reference_solution,
)
from apollo.resolution.result import ResolutionResult, ResolvedNode
from apollo.resolution.resolver import resolve_attempt

__all__ = [
    "resolve_attempt",
    "build_candidate_set",
    "candidates_from_reference_solution",
    "candidates_from_misconceptions",
    "Candidate",
    "ResolvedNode",
    "ResolutionResult",
    "METHOD_CONFIDENCE_CAP",
    "RESOLUTION_METHODS",
]
