"""Rubric: pure-function weighted grade computation.

V3 contract: takes coverage + a list of reference Node objects (typically
`reference_graph.nodes`) instead of the old reference_steps dict list.

Aggregates coverage into three axis scores:
  Procedure       weight 0.60  (mean of procedure_scores * 100)
  Justification   weight 0.25  (% of condition entries covered)
  Simplification  weight 0.15  (% of simplification entries covered)

If an axis has zero reference entries (absent), its weight is redistributed
proportionally across the remaining axes. If no axis is present, overall is 0.

Also the home of the two 0-100 → label maps: `LETTER_BANDS`/`score_to_letter`
(the teacher/research vocabulary, on every payload) and `PROFICIENCY_BANDS`/
`score_to_band` (the additive student-facing vocabulary, study-prep 2026-08-23).
Neither enters `compute_rubric`'s own output.

No LLM. Deterministic, auditable, reproducible."""
from __future__ import annotations

import math
from collections.abc import Mapping
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
#
# 2026-08-07 bimodal-fix P1.5 (decision D1) — the BOTTOM of the map is
# rescaled. The pre-fix bands gave F half the numeric scale (F = [0, 50), D =
# [50, 60)); combined with de-facto binary per-node credit over 1-3 graded
# nodes the reachable score set was ~{0, 33, 50, 67, 100}, so "missed one of two
# graded nodes" (50) read as a failing grade and a B was unreachable. Now:
#   F = [0, 30)   D = [30, 50)   C = [50, 65)
# Every A/B threshold AND the C+ threshold are UNCHANGED, and the letter SET is
# unchanged — `projections/performance_problems.letter_distribution` renders one
# teacher-facing bucket per band, so introducing a new letter (e.g. C-) would be
# a cross-repo UI surface change, which this fix deliberately avoids.
LETTER_BANDS: List[Tuple[int, str]] = [
    (97, "A+"),
    (90, "A"),
    (85, "A-"),
    (80, "B+"),
    (75, "B"),
    (70, "B-"),
    (65, "C+"),
    (50, "C"),
    (30, "D"),
    (0, "F"),
]


def score_to_letter(score: int) -> str:
    """Map an integer 0-100 score to a letter band."""
    for threshold, letter in LETTER_BANDS:
        if score >= threshold:
            return letter
    return "F"


# --------------------------------------------------------------------------- #
# Proficiency bands (study-prep 2026-08-23, spec §A.1/§A.2)                     #
# --------------------------------------------------------------------------- #
#
# The STUDENT-facing vocabulary. Letters are a teacher/research vocabulary and
# stay on every payload untouched (`letter` is never removed anywhere); `band`
# is an ADDITIVE key beside it, because on a study-prep tool a letter reads as a
# verdict where a proficiency band reads as a position.
#
# `PROFICIENCY_CUTS` is FROZEN at 50/85 by the 2026-08-23 user sign-off — these
# are decided cuts, not priors awaiting calibration. They sit on existing
# `LETTER_BANDS` floors so the two vocabularies agree at their boundaries: 85 is
# the A- floor (advanced), 50 the C floor (intermediate). They are deliberately
# NOT derived from `LETTER_BANDS` at runtime — a letter rescale must FAIL a test
# rather than silently drag the student-facing bands along with it. That
# tripwire is `overseer/tests/test_rubric_bands.py`, built on the P3.2
# ceiling-pin pattern (`test_ceiling_letter_bands.py`).
#
# Moving a cut is a COORDINATED TWO-REPO change, not one line: the student UI
# re-declares the same numbers in `ai-ta-student-ui/lib/apollo/bands.ts`
# (`ADVANCED_FLOOR = 85` / `INTERMEDIATE_FLOOR = 50`, lines 23-24) as a
# defensive fallback for a payload with no `band` token. The backend stays the
# source of truth and its token always wins, but a cut that moves here and not
# there is a silent disagreement. Nothing else in THIS tree re-declares them.
PROFICIENCY_CUTS: tuple[int, int] = (50, 85)

_INTERMEDIATE_FLOOR, _ADVANCED_FLOOR = PROFICIENCY_CUTS

# (min_score_inclusive, band) in descending order — same shape as LETTER_BANDS.
# Wire values are lowercase tokens; the display strings ("Beginner", ...) belong
# to the UI and must never appear in a payload.
PROFICIENCY_BANDS: tuple[tuple[int, str], ...] = (
    (_ADVANCED_FLOOR, "advanced"),
    (_INTERMEDIATE_FLOOR, "intermediate"),
    (0, "beginner"),
)

#: The wire vocabulary, for validating a band read back off a persisted payload.
BAND_TOKENS: frozenset[str] = frozenset(band for _threshold, band in PROFICIENCY_BANDS)


def score_to_band(score: int) -> str:
    """Map an integer 0-100 score to a student-facing proficiency band."""
    for threshold, band in PROFICIENCY_BANDS:
        if score >= threshold:
            return band
    return "beginner"


def band_from_served_overall(overall: Mapping[str, Any]) -> str | None:
    """The band for a RE-SERVED grade (browse cards, progress recents, the
    already-graded Done replay).

    Snapshot FIRST. `diagnostic_report.served_overall` is the grade the student
    was actually shown, and this module's standing rule is that re-serving
    surfaces read that snapshot rather than re-deriving, so a later cut move can
    never retroactively relabel an attempt somebody already saw. Rows graded
    BEFORE this key existed have no served band to preserve, so those alone fall
    back to `score_to_band` over the snapshot's own (verbatim, never recomputed)
    score. Returns None when there is no usable score either — exactly the rows
    on which `letter` is already None on those surfaces.
    """
    band = overall.get("band")
    if isinstance(band, str) and band in BAND_TOKENS:
        return band
    score = overall.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return None
    return score_to_band(int(round(score)))


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
