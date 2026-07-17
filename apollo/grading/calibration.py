"""WU-4C2 §6.7 — calibration metrics comparing the SHADOW graph-sim rubric vs the
OLD student-facing rubric.

A human reads these later to decide whether to flip ``APOLLO_GRAPH_GRADER_LIVE``.
PURE: reads two rubric dicts (both in the FROZEN ``compute_rubric`` shape) and
returns a frozen :class:`CalibrationMetrics` value object — mutates neither input,
calls no LLM, persists NOTHING (the §6.7 metrics are LOGGED + carried in-memory;
WU-4C key_decision #6 reuses JSONB — no dedicated calibration table in this unit).

``divergent`` is the human-review trip wire (§6.7: "one bad grade becoming
persistent memory is the failure this prevents") — it fires when the overall
letters disagree OR the overall score delta exceeds :data:`DIVERGENCE_SCORE_THRESHOLD`.
"""

from __future__ import annotations

from dataclasses import dataclass

from apollo.overseer.rubric import AXIS_WEIGHTS

# Single source for the comparison version (carried for replay parity).
CALIBRATION_VERSION: str = "calibration-v1"

# A hand-set v1 calibration knob (integer points), documented like
# ``CONTRADICTION_UNIT_PENALTY``. |overall_score_delta| beyond this trips the
# human-review ``divergent`` flag.
DIVERGENCE_SCORE_THRESHOLD: int = 10

# The fixed axis-iteration order: ``overall`` first, then the rubric's own
# AXIS_WEIGHTS keys (deterministic — Python dicts preserve insertion order).
_AXIS_ORDER: tuple[str, ...] = ("overall", *AXIS_WEIGHTS.keys())


@dataclass(frozen=True)
class AxisDelta:
    """One axis's old/shadow scores + their delta. ``None`` on a side when that
    axis is absent there (never a spurious 0-vs-0 "agreement")."""

    axis: str
    old_score: int | None
    shadow_score: int | None
    delta: int | None  # shadow - old; None if either side absent


@dataclass(frozen=True)
class CalibrationMetrics:
    """The frozen §6.7 calibration handoff. Letter agreement + per-axis deltas +
    the overall score delta + the ``divergent`` human-review trip wire."""

    letter_agreement: bool
    old_overall_letter: str
    shadow_overall_letter: str
    overall_score_delta: int
    axis_deltas: tuple[AxisDelta, ...]
    divergent: bool
    comparison_version: str


def _axis_score(rubric: dict, axis: str) -> int | None:
    """The integer score for ``axis``, or ``None`` if the axis block is marked
    absent. ``overall`` has no ``present`` flag — it is always present."""
    block = rubric.get(axis) or {}
    if axis != "overall" and not block.get("present", False):
        return None
    return block.get("score")


def _axis_delta(old_rubric: dict, shadow_rubric: dict, axis: str) -> AxisDelta:
    old_score = _axis_score(old_rubric, axis)
    shadow_score = _axis_score(shadow_rubric, axis)
    if old_score is None or shadow_score is None:
        delta: int | None = None
    else:
        delta = shadow_score - old_score
    return AxisDelta(axis=axis, old_score=old_score, shadow_score=shadow_score, delta=delta)


def compute_calibration_metrics(*, old_rubric: dict, shadow_rubric: dict) -> CalibrationMetrics:
    """Compare the SHADOW graph-sim rubric against the OLD student-facing rubric.

    PURE — reads ``["overall"]["letter"]``/``["overall"]["score"]`` + each axis
    block's ``score``/``present`` from both dicts directly (letters are already
    carried — NEVER recomputed). Returns a frozen :class:`CalibrationMetrics`."""
    old_letter = old_rubric["overall"]["letter"]
    shadow_letter = shadow_rubric["overall"]["letter"]
    letter_agreement = old_letter == shadow_letter

    overall_score_delta = shadow_rubric["overall"]["score"] - old_rubric["overall"]["score"]

    axis_deltas = tuple(_axis_delta(old_rubric, shadow_rubric, axis) for axis in _AXIS_ORDER)

    divergent = (not letter_agreement) or (abs(overall_score_delta) > DIVERGENCE_SCORE_THRESHOLD)

    return CalibrationMetrics(
        letter_agreement=letter_agreement,
        old_overall_letter=old_letter,
        shadow_overall_letter=shadow_letter,
        overall_score_delta=overall_score_delta,
        axis_deltas=axis_deltas,
        divergent=divergent,
        comparison_version=CALIBRATION_VERSION,
    )
