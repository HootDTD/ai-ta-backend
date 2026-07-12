"""WU-4A2 — grade_attempt + GradeResult: the §6 grading-core COMPARE half.

:func:`grade_attempt` is the SINGLE public callable WU-4C wires into the Done
route. It runs the §6.4 steps 10/11/13 over PURE inputs (already-canonical
``CanonicalGraph`` / ``ReferenceGraph`` from WU-4A1) — **NO Neo4j read, NO
resolver call, NO LLM, NO Postgres**. It returns one frozen :class:`GradeResult`
carrying the 3 top-line scores, the 7 sub-scores (field names matching the
``apollo_graph_comparison_runs`` columns 1:1), ``comparison_confidence`` (==1.0
in v1), a deterministically-ordered ``findings`` tuple, and ``comparison_version``.

WU-4B consumes ``GradeResult.findings`` for the transcript audit, abstention
gates, finding->event conversion (§6.5), and the runs/findings persistence; it
also supplies the persisted ``normalization_confidence`` column from resolution
method-caps. None of that is a WU-4A2 concern: this unit produces the in-memory
findings those start from and emits NO events (§6.2 + §6.5 seam).

Pure + deterministic: identical inputs yield an equal ``GradeResult`` (findings
grouped by kind then sorted within each group).
"""

from __future__ import annotations

from dataclasses import dataclass

from apollo.graph_compare.bisimilarity import bisimilarity_score
from apollo.graph_compare.canonical import CanonicalGraph, ReferenceGraph
from apollo.graph_compare.coverage import PathCoverage, coverage_result
from apollo.graph_compare.findings import (
    Finding,
    alternative_path_finding,
    contradiction_finding,
    covered_finding,
    matched_edge_finding,
    missing_edge_finding,
    missing_finding,
    unresolved_finding,
    unsupported_extra_finding,
)
from apollo.graph_compare.scores import compute_sub_scores
from apollo.graph_compare.soundness import (
    contradiction_nodes,
    is_misconception_key,
    soundness_score,
)

# The constant for the apollo_graph_comparison_runs.comparison_version column. A
# re-run at the same version is a supersede (UNIQUE constraint). Single source of
# truth; WU-4B reads it off GradeResult.comparison_version.
COMPARISON_VERSION: str = "graph-compare-v2"


@dataclass(frozen=True)
class GradeResult:
    """The frozen handoff artifact. The 10 ``*_score`` fields are named 1:1 to the
    ``apollo_graph_comparison_runs`` columns so WU-4B persists with no reshaping.

    ``comparison_confidence`` is the score-math's own value (1.0 in v1); the
    persisted confidence column is ``normalization_confidence`` (supplied by
    WU-4B from resolution method-caps), so it is carried here but NOT persisted
    by this name. There is intentionally NO ``events`` field — finding->event
    conversion is WU-4B (§6.5).

    ``soundness_applicable`` is ``False`` iff the misconception bank was
    empty/absent for this concept (D5/D6). When ``False``:
      * ``soundness_score`` and ``contradiction_score`` are ``None``;
      * ``bisimilarity_score`` holds the coverage-only fallback (== coverage_score);
      * persisted column ``soundness_applicable=false`` signals this to readers."""

    coverage_score: float
    soundness_score: float | None
    bisimilarity_score: float
    node_coverage_score: float
    edge_coverage_score: float
    scoping_score: float
    usage_score: float
    procedure_order_score: float
    dependency_score: float
    contradiction_score: float | None
    comparison_confidence: float
    findings: tuple[Finding, ...]
    soundness_applicable: bool = True
    comparison_version: str = COMPARISON_VERSION


def grade_attempt(
    student_canonical: CanonicalGraph,
    reference_graph: ReferenceGraph,
    *,
    bank_applicable: bool = True,
) -> GradeResult:
    """Grade a student's canonical graph against the reference (§6.4 10/11/13).

    Pure: coverage (max-over-paths), soundness (contradictions-only), the 7
    sub-scores, and bisimilarity, plus the in-memory finding set. No external IO.

    ``bank_applicable=False`` signals that the misconception bank was
    empty/absent for this concept (D5/D6); soundness_score and
    contradiction_score become ``None``, bisimilarity renormalizes to coverage.
    """
    # Step 10 — coverage (max over declared paths).
    coverage, winning_path, _ = coverage_result(student_canonical, reference_graph)
    # Step 11 — soundness (contradictions only; None when bank not applicable).
    soundness = soundness_score(student_canonical, bank_applicable=bank_applicable)
    # Sub-scores (contradiction sub-score also None when bank not applicable).
    sub = compute_sub_scores(
        student_canonical, reference_graph, winning_path, bank_applicable=bank_applicable
    )
    # Step 13 — bisimilarity (harmonic mean; None soundness -> coverage-only fallback).
    bisimilarity = bisimilarity_score(soundness, coverage)

    findings = _emit_findings(student_canonical, reference_graph, winning_path)

    return GradeResult(
        coverage_score=coverage,
        soundness_score=soundness,
        bisimilarity_score=bisimilarity,
        node_coverage_score=sub.node_coverage,
        edge_coverage_score=sub.edge_coverage,
        scoping_score=sub.scoping,
        usage_score=sub.usage,
        procedure_order_score=sub.procedure_order,
        dependency_score=sub.dependency,
        contradiction_score=sub.contradiction,
        comparison_confidence=1.0,  # v1 binding
        findings=findings,
        soundness_applicable=bank_applicable,
        comparison_version=COMPARISON_VERSION,
    )


def _emit_findings(
    student: CanonicalGraph,
    reference: ReferenceGraph,
    winning_path: PathCoverage,
) -> tuple[Finding, ...]:
    """Emit the §2 finding set (NO event conversion), deterministically ordered.

    Group order: covered -> missing -> alternative_path -> contradiction ->
    unsupported_extra -> unresolved -> matched_edge -> missing_edge; each group
    sorted by its primary key for reproducibility."""
    node_by_key = {n.canonical_key: n for n in student.nodes}
    ref_node_by_key = {n.canonical_key: n for n in reference.nodes}
    reference_keys = {k for p in reference.paths for k in p.canonical_keys}

    covered = [
        covered_finding(node_by_key[k])
        for k in sorted(winning_path.covered_keys)
        if k in node_by_key
    ]
    missing = [
        missing_finding(ref_node_by_key[k])
        for k in sorted(winning_path.missing_keys)
        if k in ref_node_by_key
    ]
    alternative = (
        [alternative_path_finding(winning_path.path_index, winning_path.covered_keys)]
        if winning_path.path_index != 0
        else []
    )

    contradiction = [
        contradiction_finding(n)
        for n in sorted(contradiction_nodes(student), key=lambda n: n.canonical_key)
    ]
    # Unsupported extras: S_norm nodes that match no reference key and are not a
    # misconception (honest non-detection, NOT a contradiction).
    unsupported = [
        unsupported_extra_finding(n)
        for n in sorted(student.nodes, key=lambda n: n.canonical_key)
        if n.canonical_key not in reference_keys and not is_misconception_key(n.canonical_key)
    ]
    unresolved = [
        unresolved_finding(node_id, surface)
        for node_id, surface in sorted(student.unresolved_nodes)
    ]

    matched_edges, missing_edges = _edge_findings(student, reference)

    return tuple(
        covered
        + missing
        + alternative
        + contradiction
        + unsupported
        + unresolved
        + matched_edges
        + missing_edges
    )


def _edge_findings(
    student: CanonicalGraph, reference: ReferenceGraph
) -> tuple[list[Finding], list[Finding]]:
    """Diagnostic-only edge findings: a reference edge with a matching S_norm edge
    (same type+endpoints) is MATCHED, else MISSING. Never an event; never moves
    coverage/soundness/bisimilarity (§6.2 edge demotion)."""
    student_edge_keys = {(e.edge_type, e.from_key, e.to_key) for e in student.edges}
    matched: list[Finding] = []
    missing: list[Finding] = []
    for edge in sorted(reference.edges, key=lambda e: (str(e.edge_type), e.from_key, e.to_key)):
        if (edge.edge_type, edge.from_key, edge.to_key) in student_edge_keys:
            matched.append(matched_edge_finding(edge))
        else:
            missing.append(missing_edge_finding(edge))
    return matched, missing
