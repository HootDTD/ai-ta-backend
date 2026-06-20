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

from apollo.grading.audited_grade import AuditedGrade
from apollo.graph_compare.findings import FindingKind
from apollo.resolution.result import ResolutionResult

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


def compute_normalization_confidence(
    audited_grade: AuditedGrade,
    resolution: ResolutionResult,
) -> float:
    """The conservative MIN (weakest-link) over the per-node method-cap
    ``ResolvedNode.confidence`` of the resolved nodes that backed a SCORED
    finding (covered / contradiction).

    Returns :data:`NORMALIZATION_CONFIDENCE_FLOOR_WHEN_NO_SCORED_NODES` (1.0) when
    no scored finding has a resolved backing node (mirrors
    ``audited_grade._misconception_confidences``' resolved-only lookup map)."""
    conf_by_node = {
        rn.node_id: rn.confidence
        for rn in resolution.resolved
        if rn.resolution == "resolved" and rn.confidence is not None
    }

    backing_confidences: list[float] = []
    for finding in audited_grade.findings:
        if finding.kind not in _SCORED_FINDING_KINDS:
            continue
        for node_id in finding.student_node_ids:
            if node_id in conf_by_node:
                backing_confidences.append(conf_by_node[node_id])

    if not backing_confidences:
        return NORMALIZATION_CONFIDENCE_FLOOR_WHEN_NO_SCORED_NODES
    return min(backing_confidences)
