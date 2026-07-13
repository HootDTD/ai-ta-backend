"""WU-4B3 §3 — compute_normalization_confidence: the §3 damper input.

The persisted ``apollo_graph_comparison_runs.normalization_confidence`` column is
the honest WORST-CASE confidence of the evidence that actually BACKED a SCORED
finding. It feeds ``grader_confidence = normalization_confidence ×
comparison_confidence`` (§3 line 431), so a single shaky resolution should damp
the whole run — hence MIN (weakest-link), never mean.

"Scored" findings are exactly the ones that move a top-line score: a
``covered_node`` or safeguarded ``covered_by_contraction`` (positive coverage)
and a ``contradiction`` (soundness penalty).
``unsupported_extra`` / ``unresolved`` / edge findings are diagnostic-only and
carry ZERO score weight, so their backing nodes are EXCLUDED — a low-confidence
extra must not falsely damp a confident grade.

G1 fix — TYPE-AWARE caps. The per-node value is NOT the raw method-cap but the
cap divided by the MAX confidence its node TYPE can realistically reach in
production (:data:`RESOLUTION_CEILING_BY_TYPE`). Equations have
exact/symbolic/derived paths (ceiling 1.00), so an equation that falls back to
``llm``/``fuzzy`` is genuinely suspicious; conceptual nodes (procedure_step,
condition, definition, simplification, variable_mapping) have no symbolic form
and no curated aliases on shipped problems, so they can only reach the ``llm``
tier — that IS their ceiling (0.75), not a red flag. Without this, the 0.85
abstention floor sat above the only tier conceptual nodes could reach, so every
attempt with prose abstained categorically (even perfect-coverage ones). The
node type is threaded in as a ``node_id -> node_type`` map (the resolver's
``ResolvedNode`` does not carry it); a node absent from the map defaults to the
prose ceiling.

Pure + immutable: reads frozen value objects, returns a float, mutates nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from apollo.graph_compare.findings import Finding, FindingKind
from apollo.resolution.result import ResolutionResult

if TYPE_CHECKING:
    # Import only for the public function's parameter annotation. `from __future__
    # import annotations` (above) makes that annotation a string at runtime, so
    # guarding this import breaks the latent runtime cycle that Phase 1c would
    # otherwise introduce (audited_grade imports the helper below at runtime).
    from apollo.grading.audited_grade import AuditedGrade  # pragma: no cover

# The finding kinds that MOVE a top-line score (positive coverage / soundness
# penalty). Named so "what backs a score" is ONE symbol, not literals
# scattered through the body.
_SCORED_FINDING_KINDS: frozenset[FindingKind] = frozenset(
    {
        FindingKind.COVERED_NODE,
        FindingKind.COVERED_BY_CONTRACTION,
        FindingKind.CONTRADICTION,
    }
)

# Calibration knob: when NO scored finding has a backing resolved node (a
# pure-missing or abstained-empty attempt), there is no scored evidence to damp,
# so the damper is NEUTRAL. We return 1.0 (NOT 0.0 — a 0.0 here would falsely
# zero out grader_confidence for a run that simply had nothing positive to grade).
NORMALIZATION_CONFIDENCE_FLOOR_WHEN_NO_SCORED_NODES: float = 1.0

# Max resolution confidence realistically reachable per node type (production).
# Equations have exact/symbolic/derived paths; conceptual nodes (no symbolic form,
# no curated aliases on shipped problems) bottom out at the LLM tier. Any node
# type absent here falls back to RESOLUTION_CEILING_DEFAULT.
RESOLUTION_CEILING_BY_TYPE: dict[str, float] = {"equation": 1.00}
RESOLUTION_CEILING_DEFAULT: float = 0.75  # llm cap — realistic ceiling for prose nodes


def _type_normalized_confidence(node_type: str, cap: float) -> float:
    """Judge a resolved node's per-tier ``cap`` against the MAX confidence its
    ``node_type`` can realistically reach (its ceiling), clamped to 1.0.

    An equation resolved at its exact ceiling (1.00) scores 1.0; an equation that
    fell to ``llm`` (0.75) scores 0.75 (suspicious — it had stronger paths). A
    conceptual node at its ``llm`` ceiling (0.75/0.75) scores 1.0 (at ceiling, not
    suspicious). An unknown ``node_type`` defaults to the prose ceiling."""
    ceiling = RESOLUTION_CEILING_BY_TYPE.get(node_type, RESOLUTION_CEILING_DEFAULT)
    return min(1.0, cap / ceiling) if ceiling > 0 else 1.0


def _normalization_confidence_over(
    findings: tuple[Finding, ...],
    resolution: ResolutionResult,
    node_type_by_id: Mapping[str, str] | None = None,
) -> float:
    """The MIN (weakest-link) over the TYPE-NORMALIZED per-node method-cap of the
    resolved nodes that backed a SCORED finding in ``findings``.

    Each backing node's raw ``ResolvedNode.confidence`` (its method-cap) is passed
    through :func:`_type_normalized_confidence` — cap / the node TYPE's realistic
    ceiling, clamped to 1.0 — BEFORE the MIN, so the value reflects "resolved
    within X% of what this node type could achieve", not a flat absolute (the G1
    fix). ``node_type_by_id`` maps each scored node_id to its ontology
    ``node_type``; a node absent from the map (or ``node_type_by_id is None``)
    defaults to the prose ceiling (see :func:`_type_normalized_confidence`).

    Pure-helper form taking the findings tuple DIRECTLY (not via an
    :class:`AuditedGrade`), so Phase 1c's ``build_audited_grade`` can compute nc
    over POST-rewrite findings BEFORE the frozen ``AuditedGrade`` is constructed.
    Returns :data:`NORMALIZATION_CONFIDENCE_FLOOR_WHEN_NO_SCORED_NODES` (1.0) when
    no scored finding has a resolved backing node."""
    types = node_type_by_id or {}
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
                node_type = types.get(node_id, "")
                backing_confidences.append(
                    _type_normalized_confidence(node_type, conf_by_node[node_id])
                )

    if not backing_confidences:
        return NORMALIZATION_CONFIDENCE_FLOOR_WHEN_NO_SCORED_NODES
    return min(backing_confidences)


def compute_normalization_confidence(
    audited_grade: AuditedGrade,
    resolution: ResolutionResult,
    node_type_by_id: Mapping[str, str] | None = None,
) -> float:
    """The conservative MIN (weakest-link) over the TYPE-NORMALIZED per-node
    method-cap of the resolved nodes that backed a SCORED finding (covered /
    contradiction). See :func:`_normalization_confidence_over` for the
    type-normalization (G1 fix).

    Thin delegator to :func:`_normalization_confidence_over` over
    ``audited_grade.findings``. ``node_type_by_id`` (the ``node_id -> node_type``
    map sourced from the attempt's student nodes) is OPTIONAL and keyword-default
    ``None`` — existing callers keep working; production callers
    (``done_grading.py`` / ``audited_grade.py``) thread it in so each scored node
    is judged against its type ceiling. The persisted
    ``apollo_graph_comparison_runs.normalization_confidence`` value is this return.
    Returns :data:`NORMALIZATION_CONFIDENCE_FLOOR_WHEN_NO_SCORED_NODES` (1.0) when
    no scored finding has a resolved backing node."""
    return _normalization_confidence_over(audited_grade.findings, resolution, node_type_by_id)
