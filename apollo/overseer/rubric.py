"""Rubric: pure-function weighted grade computation.

V3 contract: takes coverage + a list of reference Node objects (typically
`reference_graph.nodes`) instead of the old reference_steps dict list.

Aggregates coverage into three axis scores:
  Procedure       weight 0.60  (mean of procedure_scores * 100)
  Justification   weight 0.25  (% of condition entries covered)
  Simplification  weight 0.15  (% of simplification entries covered)

If an axis has zero reference entries (absent), its weight is redistributed
proportionally across the remaining axes. If no axis is present, overall is 0.

No LLM. Deterministic, auditable, reproducible."""
from __future__ import annotations

import math
from typing import Any, Dict, List, Tuple

from apollo.ontology import Node, NodeType

def _finite_score(v: Any) -> float:
    """Coerce a score value to a finite float in [0, 1]; NaN/inf become 0."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(f):
        return 0.0
    return max(0.0, min(1.0, f))


# Class 2 Phase 2 (P2.8): the misconception axis enters at 5% of the
# overall, taken proportionally from the existing 60/25/15. The values
# below are the post-rebalance weights. In the degenerate case where
# the misconception axis is absent (no detections in the attempt), the
# absent-axis redistribution restores the original 60/25/15 ratio
# exactly — the rubric is byte-identical to the pre-P2.8 result.
AXIS_WEIGHTS: Dict[str, float] = {
    "procedure": 0.60 * 0.95,        # 0.57
    "justification": 0.25 * 0.95,    # 0.2375
    "simplification": 0.15 * 0.95,   # 0.1425
    "misconception_corrected": 0.05,
}

# (min_score_inclusive, letter) in descending order.
LETTER_BANDS: List[Tuple[int, str]] = [
    (97, "A+"),
    (90, "A"),
    (85, "A-"),
    (80, "B+"),
    (75, "B"),
    (70, "B-"),
    (65, "C+"),
    (60, "C"),
    (50, "D"),
    (0, "F"),
]


def score_to_letter(score: int) -> str:
    """Map an integer 0-100 score to a letter band."""
    for threshold, letter in LETTER_BANDS:
        if score >= threshold:
            return letter
    return "F"


def _axis_for(node_type: str) -> str | None:
    if node_type == "procedure_step":
        return "procedure"
    if node_type == "condition":
        return "justification"
    if node_type == "simplification":
        return "simplification"
    return None  # equation feeds the solver; definition/variable_mapping are not graded.


def compute_rubric(
    coverage: Dict[str, Any],
    reference_nodes: List[Node],
    *,
    misconception_scores: Dict[str, float] | None = None,
) -> Dict[str, Any]:
    """Return rubric dict with per-axis scores and overall letter.

    `misconception_scores` (Class 2 Phase 2) is a per-bank-code score
    map (e.g. `{"no_density": 1.0, "wrong_law": 0.5}`) where:
      - 1.0 = detected and resolved (no further detection in the last
        >= 2 turns of the attempt)
      - 0.5 = detected and unresolved
      - never-detected codes do not appear in this dict (no penalty,
        no bonus)
    The axis is "present" iff this dict is non-empty. When absent, the
    overall is byte-identical to the pre-P2.8 60/25/15 rubric.

    Shape:
      {
        "overall":                {"score": int, "letter": str},
        "procedure":              {"score": int, "letter": str, "present": bool},
        "justification":          {"score": int, "letter": str, "present": bool},
        "simplification":         {"score": int, "letter": str, "present": bool},
        "misconception_corrected":{"score": int, "letter": str, "present": bool,
                                   "detected": int, "resolved": int},
      }
    """
    per_step = coverage.get("per_step", {})
    procedure_scores = coverage.get("procedure_scores", {})

    # Bucket reference nodes by axis.
    axis_refs: Dict[str, List[Node]] = {a: [] for a in AXIS_WEIGHTS}
    for ref in reference_nodes:
        axis = _axis_for(ref.node_type)
        if axis is not None:
            axis_refs[axis].append(ref)

    # Compute per-axis 0-100 score (None if axis is absent).
    axis_raw: Dict[str, float | None] = {}

    # Procedure: mean of per-step 0-1 scores * 100.
    proc_refs = axis_refs["procedure"]
    if proc_refs:
        scores = [_finite_score(procedure_scores.get(r.node_id, 0.0)) for r in proc_refs]
        axis_raw["procedure"] = (sum(scores) / len(scores)) * 100.0
    else:
        axis_raw["procedure"] = None

    # Binary axes: % covered.
    for axis in ("justification", "simplification"):
        refs = axis_refs[axis]
        if refs:
            covered = sum(1 for r in refs if per_step.get(r.node_id) == "covered")
            axis_raw[axis] = (covered / len(refs)) * 100.0
        else:
            axis_raw[axis] = None

    # Misconception axis: weighted average of per-code resolution scores.
    misc_resolved = 0
    misc_detected = 0
    if misconception_scores:
        misc_detected = len(misconception_scores)
        misc_resolved = sum(1 for s in misconception_scores.values() if s >= 1.0)
        axis_raw["misconception_corrected"] = (
            sum(misconception_scores.values()) / misc_detected
        ) * 100.0
    else:
        axis_raw["misconception_corrected"] = None

    # Compute overall with absent-axis redistribution.
    present_weights = {a: AXIS_WEIGHTS[a] for a, v in axis_raw.items() if v is not None}
    total_weight = sum(present_weights.values())
    if total_weight == 0.0:
        overall_score = 0.0
    else:
        overall_score = sum(
            axis_raw[a] * (w / total_weight) for a, w in present_weights.items()
        )

    def _axis_block(axis: str) -> Dict[str, Any]:
        raw = axis_raw[axis]
        if raw is None:
            return {"score": 0, "letter": "F", "present": False}
        score_int = int(round(raw))
        return {"score": score_int, "letter": score_to_letter(score_int), "present": True}

    overall_int = int(round(overall_score))
    misc_block = _axis_block("misconception_corrected")
    if misc_block["present"]:
        misc_block["detected"] = misc_detected
        misc_block["resolved"] = misc_resolved
    else:
        misc_block["detected"] = 0
        misc_block["resolved"] = 0
    return {
        "overall": {"score": overall_int, "letter": score_to_letter(overall_int)},
        "procedure": _axis_block("procedure"),
        "justification": _axis_block("justification"),
        "simplification": _axis_block("simplification"),
        "misconception_corrected": misc_block,
    }
