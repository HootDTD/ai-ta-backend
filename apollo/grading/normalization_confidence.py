"""WU-4B3 §3 — compute_normalization_confidence: the §3 damper input.

The persisted ``apollo_graph_comparison_runs.normalization_confidence`` column is
the honest WORST-CASE confidence of the evidence that actually BACKED a SCORED
finding. It feeds ``grader_confidence = normalization_confidence ×
comparison_confidence`` (§3 line 431), so a single shaky resolution should damp
the whole run — hence MIN (weakest-link), never mean.

"Scored" findings are exactly the ones that move a top-line score: a
``covered_node`` (positive coverage) and a ``contradiction`` (soundness penalty).
``unsupported_extra`` / ``unresolved`` / edge findings are diagnostic-only and
carry ZERO score weight, so their backing nodes are EXCLUDED — a low-confidence
extra must not falsely damp a confident grade.

Pure + immutable: reads frozen value objects, returns a float, mutates nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from apollo.graph_compare.findings import Finding, FindingKind
from apollo.resolution.result import ResolutionResult

if TYPE_CHECKING:
    # Import only for the public function's parameter annotation. `from __future__
    # import annotations` (above) makes that annotation a string at runtime, so
    # guarding this import breaks the latent runtime cycle that Phase 1c would
    # otherwise introduce (audited_grade imports the helper below at runtime).
    from apollo.grading.audited_grade import AuditedGrade  # pragma: no cover

# The two finding kinds that MOVE a top-line score (positive coverage /
# soundness penalty). Named so "what backs a score" is ONE symbol, not literals
# scattered through the body.
_SCORED_FINDING_KINDS: frozenset[FindingKind] = frozenset(
    {FindingKind.COVERED_NODE, FindingKind.CONTRADICTION}
)

# Calibration knob: when NO scored finding has a backing resolved node (a
# pure-missing or abstained-empty attempt), there is no scored evidence to damp,
# so the damper is NEUTRAL. We return 1.0 (NOT 0.0 — a 0.0 here would falsely
# zero out grader_confidence for a run that simply had nothing positive to grade).
NORMALIZATION_CONFIDENCE_FLOOR_WHEN_NO_SCORED_NODES: float = 1.0


def _normalization_confidence_over(
    findings: tuple[Finding, ...],
    resolution: ResolutionResult,
) -> float:
    """The MIN (weakest-link) over the per-node method-cap ``ResolvedNode``
    confidence of the resolved nodes that backed a SCORED finding in ``findings``.

    Pure-helper form taking the findings tuple DIRECTLY (not via an
    :class:`AuditedGrade`), so Phase 1c's ``build_audited_grade`` can compute nc
    over POST-rewrite findings BEFORE the frozen ``AuditedGrade`` is constructed.
    The body is the historical ``compute_normalization_confidence`` body verbatim
    — the value is unchanged. Returns
    :data:`NORMALIZATION_CONFIDENCE_FLOOR_WHEN_NO_SCORED_NODES` (1.0) when no scored
    finding has a resolved backing node."""
    conf_by_node = {
        rn.node_id: rn.confidence
        for rn in resolution.resolved
        if rn.resolution == "resolved" and rn.confidence is not None
    }

    backing_confidences: list[float] = []
    for finding in findings:
        if finding.kind not in _SCORED_FINDING_KINDS:
            continue
        for node_id in finding.student_node_ids:
            if node_id in conf_by_node:
                backing_confidences.append(conf_by_node[node_id])

    if not backing_confidences:
        return NORMALIZATION_CONFIDENCE_FLOOR_WHEN_NO_SCORED_NODES
    return min(backing_confidences)


def compute_normalization_confidence(
    audited_grade: AuditedGrade,
    resolution: ResolutionResult,
) -> float:
    """The conservative MIN (weakest-link) over the per-node method-cap
    ``ResolvedNode.confidence`` of the resolved nodes that backed a SCORED
    finding (covered / contradiction).

    Thin delegator to :func:`_normalization_confidence_over` over
    ``audited_grade.findings`` — signature, name, and returned value are
    UNCHANGED (the public WU-4B3 contract: the persisted
    ``apollo_graph_comparison_runs.normalization_confidence`` value). Returns
    :data:`NORMALIZATION_CONFIDENCE_FLOOR_WHEN_NO_SCORED_NODES` (1.0) when no scored
    finding has a resolved backing node."""
    return _normalization_confidence_over(audited_grade.findings, resolution)
