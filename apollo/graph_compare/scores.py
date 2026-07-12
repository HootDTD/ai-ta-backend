"""WU-4A2 — the 7 rubric sub-scores (§6.2).

Each sub-score is a PURE ratio in [0, 1] over the canonical graphs and **never
emits an event** (§6.2 edge demotion + §6.5 seam). The seven dimensions:

  * ``node_coverage`` — the winning path's covered/total (the node dimension;
    equals the top-line coverage in v1, both columns exist in the schema).
  * ``edge_coverage`` — matched reference edges / total reference edges, where a
    match is keyed on ``(edge_type, from_key, to_key)``. **explicit outweighs
    inferred:** an inferred-only match counts :data:`INFERRED_EDGE_WEIGHT`
    (0.5); a match with any explicit student edge counts 1.0.
  * ``scoping`` — matched SCOPES edges / reference SCOPES edges.
  * ``usage`` — matched USES edges / reference USES edges.
  * ``procedure_order`` — penalize ONLY true PRECEDES inversions. The reference
    carries DEPENDS_ON (not PRECEDES) edges (WU-4A1 ``build_reference_canonical``),
    so the true reference ORDER is read from the WINNING path's SEQUENCE over
    procedure_step keys. For each reference-ordered pair (A before B) where BOTH
    are present in S_norm, an inversion is an S_norm PRECEDES edge B->A. Score =
    ``1 - inversions/comparable_pairs``; a MISSING stated order is NOT penalized;
    vacuous 1.0 when no comparable ordered pairs exist.
  * ``dependency`` — LOWEST weight; direction-loose any->any DEPENDS_ON: matched
    (unordered key-pair) reference DEPENDS_ON edges / total. The weighting is a
    downstream rubric-aggregation concern (WU-4C); here it is just computed.
    DAG-0 leaves this intentionally direction-invariant; the separate
    ``edge_coverage`` dimension above compares unified directed tuples exactly.
  * ``contradiction`` — the soundness contradiction dimension surfaced as a
    [0,1] sub-score (intentionally equal to soundness in v1).

Every "vacuous → 1.0" branch (reference has zero edges of that type) is explicit
so no dimension is ever a ``ZeroDivisionError`` or NaN.

Documented v1 constant: ``INFERRED_EDGE_WEIGHT = 0.5``. Pure + deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass

from apollo.graph_compare.canonical import CanonicalEdge, CanonicalGraph, ReferenceGraph
from apollo.graph_compare.coverage import PathCoverage
from apollo.graph_compare.soundness import contradiction_nodes, contradiction_penalty
from apollo.ontology.edges import EdgeType

# v1 calibration knob (§6.2 "explicit outweighs inferred"): a reference edge
# matched ONLY by an inferred student edge counts this much toward coverage.
INFERRED_EDGE_WEIGHT: float = 0.5


@dataclass(frozen=True)
class SubScores:
    """The 7 rubric sub-scores; ``core.py`` maps these onto the column-named
    ``GradeResult`` fields."""

    node_coverage: float
    edge_coverage: float
    scoping: float
    usage: float
    procedure_order: float
    dependency: float
    contradiction: float | None


def compute_sub_scores(
    student: CanonicalGraph,
    reference: ReferenceGraph,
    winning_path: PathCoverage,
    *,
    bank_applicable: bool = True,
) -> SubScores:
    """Compute all 7 sub-scores over the two canonical graphs + the winning path.

    ``bank_applicable=False`` sets the ``contradiction`` sub-score to ``None``
    (D5/D6 — an empty/absent misconception bank means no ``misc.*`` nodes can
    ever resolve, making a "0 contradictions → 1.0" count meaningless). Callers
    (``core.py``) must thread the same flag through from the orchestrator."""
    contradiction: float | None = (
        None
        if not bank_applicable
        else 1.0 - contradiction_penalty(len(contradiction_nodes(student)))
    )
    return SubScores(
        node_coverage=winning_path.score,
        edge_coverage=_edge_coverage(student, reference, edge_type=None),
        scoping=_edge_coverage(student, reference, edge_type=EdgeType.SCOPES),
        usage=_edge_coverage(student, reference, edge_type=EdgeType.USES),
        procedure_order=_procedure_order(student, reference, winning_path),
        dependency=_dependency(student, reference),
        contradiction=contradiction,
    )


def _edge_coverage(
    student: CanonicalGraph,
    reference: ReferenceGraph,
    *,
    edge_type: EdgeType | None,
) -> float:
    """Weighted matched / total over reference edges (optionally of one type).

    A reference edge matches iff an S_norm edge with the same
    ``(edge_type, from_key, to_key)`` exists; an explicit student match scores
    1.0, an inferred-only match scores :data:`INFERRED_EDGE_WEIGHT`. Vacuous
    (no reference edges in scope) -> 1.0."""
    ref_edges = _edges_of_type(reference.edges, edge_type)
    if not ref_edges:
        return 1.0
    # Best provenance per (type, from, to) among S_norm edges: explicit > inferred.
    best: dict[tuple[EdgeType, str, str], float] = {}
    for e in _edges_of_type(student.edges, edge_type):
        weight = 1.0 if e.provenance == "explicit" else INFERRED_EDGE_WEIGHT
        triple = (e.edge_type, e.from_key, e.to_key)
        best[triple] = max(best.get(triple, 0.0), weight)
    total = sum(best.get((re.edge_type, re.from_key, re.to_key), 0.0) for re in ref_edges)
    return total / len(ref_edges)


def _procedure_order(
    student: CanonicalGraph,
    reference: ReferenceGraph,
    winning_path: PathCoverage,
) -> float:
    """Penalize only true PRECEDES inversions on the winning path.

    Comparable pairs: reference-ordered (A before B) procedure_step keys both
    present in S_norm. An inversion is an S_norm PRECEDES edge B->A. Missing
    stated order is NOT penalized; vacuous 1.0 when no comparable pairs."""
    proc_keys = _procedure_step_keys(reference)
    # The reference order is the winning path's procedure_step subsequence. Every
    # key in `ordered` is a COVERED key, so it is guaranteed present in S_norm —
    # no further presence guard is needed (comparable pairs are exactly the
    # ordered pairs over the covered procedure steps).
    ordered = [k for k in winning_path.covered_keys if k in proc_keys]
    student_precedes = {
        (e.from_key, e.to_key) for e in student.edges if e.edge_type == EdgeType.PRECEDES
    }
    comparable = 0
    inversions = 0
    for i, a in enumerate(ordered):
        for b in ordered[i + 1 :]:
            # A precedes B in the reference; an inversion is the student stating
            # the reverse order (a PRECEDES edge B->A). A missing stated order is
            # NOT an inversion (and so is never penalized).
            comparable += 1
            if (b, a) in student_precedes:
                inversions += 1
    if comparable == 0:
        return 1.0
    return 1.0 - inversions / comparable


def _dependency(student: CanonicalGraph, reference: ReferenceGraph) -> float:
    """Direction-loose DEPENDS_ON match: matched unordered key-pairs / total.
    Lowest-weight dimension (the weight applies downstream, WU-4C). Vacuous 1.0
    when the reference has no DEPENDS_ON edges."""
    ref_pairs = [
        frozenset((e.from_key, e.to_key))
        for e in reference.edges
        if e.edge_type == EdgeType.DEPENDS_ON
    ]
    if not ref_pairs:
        return 1.0
    student_pairs = {
        frozenset((e.from_key, e.to_key))
        for e in student.edges
        if e.edge_type == EdgeType.DEPENDS_ON
    }
    matched = sum(1 for p in ref_pairs if p in student_pairs)
    return matched / len(ref_pairs)


def _edges_of_type(
    edges: tuple[CanonicalEdge, ...], edge_type: EdgeType | None
) -> tuple[CanonicalEdge, ...]:
    if edge_type is None:
        return edges
    return tuple(e for e in edges if e.edge_type == edge_type)


def _procedure_step_keys(reference: ReferenceGraph) -> set[str]:
    return {n.canonical_key for n in reference.nodes if n.node_type == "procedure_step"}
