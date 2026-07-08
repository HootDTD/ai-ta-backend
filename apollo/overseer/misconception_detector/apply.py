"""Apply a :class:`MergeOutcome` to the artifact composite and the rubric.

Frozen contract: ``docs/_archive/plans/2026-07-08-apollo-misconception-detector-plan.md``
section 5.7, amended by A4 (band-system separation) and A8 (inline rounding,
no cross-package import of ``apollo.grading.composite._round_like_composite``).

Two pure functions, two different "ceilings":

* :func:`apply_penalty` operates on the artifact's 0..1 ``composite`` float.
  ``outcome.ceiling_applied`` caps this value at (or below) the
  SCORECARD/named-band ceiling ``CEILING_COMPOSITE`` (0.84 — "below the named
  Strong band"). This is a scorecard-projection concept and belongs to this
  function alone (A4).
* :func:`rubric_overall_after_penalty` operates on the rubric's LETTER-band
  ``overall`` block (``{"score": int 0-100, "letter": str}``). It reduces
  ``overall.score`` by the penalty (scaled to 0-100) and recomputes
  ``overall.letter`` via ``rubric.py::score_to_letter`` on the NEW score. It
  does **not** know about ``CEILING_COMPOSITE`` or any named band — any
  "ceiling" here is purely the arithmetic score reduction (A4).

Both functions are pure: no IO, no mutation of inputs, new objects returned.
"""

from __future__ import annotations

from apollo.overseer.misconception_detector.config import CEILING_COMPOSITE
from apollo.overseer.misconception_detector.types import MergeOutcome
from apollo.overseer.rubric import score_to_letter

# Composite scores are rounded to this many decimal places, matching
# apollo/grading/composite.py's _ROUND_DP so two calls with equivalent
# inputs always compare equal (A8 — replicated here, never imported, so
# this module has no dependency on apollo.grading.composite).
_ROUND_DP = 6


def _clamp_round(value: float) -> float:
    """Replicate ``round(max(0.0, min(1.0, x)), 6)`` inline (A8)."""
    return round(max(0.0, min(1.0, value)), _ROUND_DP)


def apply_penalty(
    *,
    composite: float,
    outcome: MergeOutcome,
    ceiling: float = CEILING_COMPOSITE,
) -> float:
    """Pure. Subtract the misconception penalty from ``composite``, clamp to
    ``[0, 1]``, then — if ``outcome.ceiling_applied`` — cap the result at
    ``ceiling`` (never raise it). Rounded via the inline
    ``round(max(0.0, min(1.0, x)), 6)`` convention (A8).

    An empty/zero-penalty, non-ceiling outcome returns the (clamped/rounded)
    input composite unchanged. ``ceiling`` is the NAMED-band (scorecard)
    ceiling — it belongs to this function only, never to
    :func:`rubric_overall_after_penalty` (A4).
    """
    penalized = composite - outcome.misconception_penalty
    result = _clamp_round(penalized)
    if outcome.ceiling_applied:
        result = min(result, _clamp_round(ceiling))
    return result


def rubric_overall_after_penalty(
    rubric: dict,
    outcome: MergeOutcome,
    *,
    ceiling: float = CEILING_COMPOSITE,
) -> dict:
    """Return a NEW rubric dict whose ``overall.score`` is reduced by the
    penalty (scaled to the 0-100 range) and whose ``overall.letter`` is
    RECOMPUTED from the new score via ``rubric.py::score_to_letter``
    (LETTER bands, A4). This function does not apply ``CEILING_COMPOSITE``
    or any named-band (Strong/Proficient/Developing) concept to the rubric
    — that ceiling lives only on the artifact composite in
    :func:`apply_penalty`, consumed by ``render_scorecard``. The ``ceiling``
    kwarg is accepted only for signature symmetry / future callers and is
    intentionally unused for any named-band clamping here.

    The input ``rubric`` dict (and its nested ``overall`` dict) are never
    mutated — a shallow copy of ``rubric`` is returned with a brand-new
    ``overall`` dict; all other axis blocks are passed through unchanged
    (same nested dict objects, since we never mutate them either).
    """
    del ceiling  # accepted for signature symmetry only; not used (A4).

    overall = rubric["overall"]
    original_score = int(overall["score"])

    points_off = round(outcome.misconception_penalty * 100)
    new_score = max(0, min(100, original_score - points_off))

    new_rubric = dict(rubric)
    new_rubric["overall"] = {
        "score": new_score,
        "letter": score_to_letter(new_score),
    }
    return new_rubric
