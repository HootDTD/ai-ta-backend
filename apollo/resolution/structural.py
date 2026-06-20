"""WU-3C2 — structural type-compat HARD constraint + the working ScoredMatch.

§5 step 3 names two structural roles; v1 wires only the HARD one:

- :func:`type_compatible` — the HARD constraint (a student node only resolves
  to a candidate of the SAME node type; condition <-> condition only). This is
  the structural signal the live resolver enforces, alongside misconception
  competition (``competition.py``) as the anti-over-normalization guardrail.

Neighborhood corroboration (§5 steps 2-3 — prioritizing an anchored node's
edge-neighbors and boosting a match's confidence from agreeing neighbors) is
DEFERRED to a WU-4A-era refinement. It is NOT wired in v1: student graphs are
edge-sparse, so the propagate-and-veto seam is deliberately left unbuilt rather
than half-wired. The live resolver performs NO neighborhood corroboration.

All pure + immutable.
"""

from __future__ import annotations

from dataclasses import dataclass

from apollo.ontology.nodes import NodeType
from apollo.resolution.candidates import Candidate


@dataclass(frozen=True)
class ScoredMatch:
    """A student-node -> candidate match with a working score, before the
    final method-cap is applied. Immutable.

    ``score`` is the RAW competition/assignment ranking signal (1.0 for an exact
    alias hit, the ``token_set_ratio`` for a fuzzy hit); the reported confidence
    is re-derived from ``METHOD_CONFIDENCE_CAP[method]`` by the resolver."""

    node_id: str
    candidate: Candidate
    method: str
    score: float


def type_compatible(student_node_type: NodeType, candidate: Candidate) -> bool:
    """HARD type constraint (§5): a student node only resolves to a candidate of
    the SAME node type. No cross-type resolution, ever — a condition never
    resolves to an equation candidate even at the top text score."""
    return student_node_type == candidate.node_type
