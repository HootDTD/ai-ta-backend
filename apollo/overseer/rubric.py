"""Rubric: pure-function weighted grade computation.

Aggregates coverage into four axis scores:
  Procedure       weight 0.50 (mean of procedure_scores * 100)
  Justification   weight 0.25 (% of condition entries covered)
  Simplification  weight 0.125 (% of simplification entries covered)
  Variables       weight 0.125 (% of definition + variable_mapping covered)

If an axis has zero reference entries (absent), its weight is redistributed
proportionally across the remaining axes. If no axis is present, overall is 0.

No LLM. Deterministic, auditable, reproducible."""
from __future__ import annotations

import math
from typing import Any, Dict, List, Tuple

def _finite_score(v: Any) -> float:
    """Coerce a score value to a finite float in [0, 1]; NaN/inf become 0."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(f):
        return 0.0
    return max(0.0, min(1.0, f))


AXIS_WEIGHTS: Dict[str, float] = {
    "procedure": 0.50,
    "justification": 0.25,
    "simplification": 0.125,
    "variables": 0.125,
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


def _axis_for(entry_type: str) -> str | None:
    if entry_type == "procedure_step":
        return "procedure"
    if entry_type == "condition":
        return "justification"
    if entry_type == "simplification":
        return "simplification"
    if entry_type in ("definition", "variable_mapping"):
        return "variables"
    return None  # equation entries are not graded by the rubric; they feed the solver.


def compute_rubric(
    coverage: Dict[str, Any],
    reference_steps: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Return rubric dict with per-axis scores and overall letter.

    Shape:
      {
        "overall":        {"score": int, "letter": str},
        "procedure":      {"score": int, "letter": str, "present": bool},
        "justification":  {"score": int, "letter": str, "present": bool},
        "simplification": {"score": int, "letter": str, "present": bool},
        "variables":      {"score": int, "letter": str, "present": bool},
      }
    """
    per_step = coverage.get("per_step", {})
    procedure_scores = coverage.get("procedure_scores", {})

    # Bucket reference steps by axis.
    axis_refs: Dict[str, List[Dict[str, Any]]] = {a: [] for a in AXIS_WEIGHTS}
    for ref in reference_steps:
        axis = _axis_for(ref.get("entry_type", ""))
        if axis is not None:
            axis_refs[axis].append(ref)

    # Compute per-axis 0-100 score (None if axis is absent).
    axis_raw: Dict[str, float | None] = {}

    # Procedure: mean of per-step 0-1 scores * 100.
    proc_refs = axis_refs["procedure"]
    if proc_refs:
        scores = [_finite_score(procedure_scores.get(r["id"], 0.0)) for r in proc_refs]
        axis_raw["procedure"] = (sum(scores) / len(scores)) * 100.0
    else:
        axis_raw["procedure"] = None

    # Binary axes: % covered.
    for axis in ("justification", "simplification", "variables"):
        refs = axis_refs[axis]
        if refs:
            covered = sum(1 for r in refs if per_step.get(r["id"]) == "covered")
            axis_raw[axis] = (covered / len(refs)) * 100.0
        else:
            axis_raw[axis] = None

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
    return {
        "overall": {"score": overall_int, "letter": score_to_letter(overall_int)},
        "procedure": _axis_block("procedure"),
        "justification": _axis_block("justification"),
        "simplification": _axis_block("simplification"),
        "variables": _axis_block("variables"),
    }
