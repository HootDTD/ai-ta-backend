"""WU-4C2 §6.7 — calibration metrics (shadow rubric vs OLD student-facing rubric).

Pure unit tests. Two rubric dicts built as literals matching the
``compute_rubric`` shape; no LLM, no DB, no container. Pins letter agreement,
per-axis + overall score deltas, the axis-absent None handling (no spurious
0-vs-0 agreement), and the ``divergent`` human-review trip wire.
"""

from __future__ import annotations

from apollo.grading.calibration import (
    CALIBRATION_VERSION,
    DIVERGENCE_SCORE_THRESHOLD,
    AxisDelta,
    CalibrationMetrics,
    compute_calibration_metrics,
)


def _axis(score: int, letter: str, present: bool = True) -> dict:
    return {"score": score, "letter": letter, "present": present}


def _rubric(
    *,
    overall: int,
    overall_letter: str,
    procedure: dict | None = None,
    justification: dict | None = None,
    simplification: dict | None = None,
    misconception: dict | None = None,
) -> dict:
    return {
        "overall": {"score": overall, "letter": overall_letter},
        "procedure": procedure or _axis(0, "F", present=False),
        "justification": justification or _axis(0, "F", present=False),
        "simplification": simplification or _axis(0, "F", present=False),
        "misconception_corrected": misconception or _axis(0, "F", present=False),
    }


def test_letter_agreement_true_when_letters_match():
    old = _rubric(overall=76, overall_letter="B")
    shadow = _rubric(overall=78, overall_letter="B")
    out = compute_calibration_metrics(old_rubric=old, shadow_rubric=shadow)
    assert isinstance(out, CalibrationMetrics)
    assert out.letter_agreement is True
    assert out.old_overall_letter == "B"
    assert out.shadow_overall_letter == "B"


def test_letter_agreement_false_when_letters_differ():
    old = _rubric(overall=92, overall_letter="A")
    shadow = _rubric(overall=62, overall_letter="C")
    out = compute_calibration_metrics(old_rubric=old, shadow_rubric=shadow)
    assert out.letter_agreement is False
    assert out.divergent is True


def test_overall_score_delta_is_shadow_minus_old():
    old = _rubric(overall=70, overall_letter="B-")
    shadow = _rubric(overall=85, overall_letter="A-")
    out = compute_calibration_metrics(old_rubric=old, shadow_rubric=shadow)
    assert out.overall_score_delta == 15


def test_axis_delta_present_both_sides():
    old = _rubric(overall=70, overall_letter="B-", procedure=_axis(60, "C", present=True))
    shadow = _rubric(overall=80, overall_letter="B+", procedure=_axis(80, "B+", present=True))
    out = compute_calibration_metrics(old_rubric=old, shadow_rubric=shadow)
    proc = next(d for d in out.axis_deltas if d.axis == "procedure")
    assert isinstance(proc, AxisDelta)
    assert proc.old_score == 60
    assert proc.shadow_score == 80
    assert proc.delta == 20


def test_axis_delta_absent_one_side_is_none():
    old = _rubric(overall=70, overall_letter="B-", procedure=_axis(60, "C", present=True))
    shadow = _rubric(overall=70, overall_letter="B-", procedure=_axis(0, "F", present=False))
    out = compute_calibration_metrics(old_rubric=old, shadow_rubric=shadow)
    proc = next(d for d in out.axis_deltas if d.axis == "procedure")
    assert proc.old_score == 60
    assert proc.shadow_score is None
    assert proc.delta is None


def test_divergent_trips_on_score_threshold():
    # Letters match but |delta| = 11 (> the threshold of 10) -> divergent.
    assert DIVERGENCE_SCORE_THRESHOLD == 10
    old = _rubric(overall=75, overall_letter="B")
    shadow = _rubric(overall=86, overall_letter="B")  # both within "B"-ish? use same letter
    # force identical letters to isolate the score-threshold trip
    shadow["overall"]["letter"] = "B"
    out = compute_calibration_metrics(old_rubric=old, shadow_rubric=shadow)
    assert out.letter_agreement is True
    assert abs(out.overall_score_delta) == 11
    assert out.divergent is True


def test_not_divergent_when_close_and_letters_match():
    old = _rubric(overall=75, overall_letter="B")
    shadow = _rubric(overall=78, overall_letter="B")
    out = compute_calibration_metrics(old_rubric=old, shadow_rubric=shadow)
    assert out.letter_agreement is True
    assert abs(out.overall_score_delta) == 3
    assert out.divergent is False


def test_calibration_version_constant():
    assert CALIBRATION_VERSION == "calibration-v1"
    old = _rubric(overall=75, overall_letter="B")
    shadow = _rubric(overall=75, overall_letter="B")
    out = compute_calibration_metrics(old_rubric=old, shadow_rubric=shadow)
    assert out.comparison_version == CALIBRATION_VERSION


def test_axis_deltas_cover_all_axes_deterministic_order():
    old = _rubric(
        overall=75,
        overall_letter="B",
        procedure=_axis(70, "B-", present=True),
        justification=_axis(80, "B+", present=True),
        simplification=_axis(60, "C", present=True),
        misconception=_axis(100, "A+", present=True),
    )
    shadow = _rubric(
        overall=75,
        overall_letter="B",
        procedure=_axis(70, "B-", present=True),
        justification=_axis(80, "B+", present=True),
        simplification=_axis(60, "C", present=True),
        misconception=_axis(100, "A+", present=True),
    )
    out = compute_calibration_metrics(old_rubric=old, shadow_rubric=shadow)
    axes = [d.axis for d in out.axis_deltas]
    assert axes == [
        "overall",
        "procedure",
        "justification",
        "simplification",
        "misconception_corrected",
    ]
    # all deltas zero (identical rubrics)
    assert all(d.delta == 0 for d in out.axis_deltas)


def test_inputs_not_mutated():
    old = _rubric(overall=70, overall_letter="B-", procedure=_axis(60, "C", present=True))
    shadow = _rubric(overall=85, overall_letter="A-", procedure=_axis(90, "A", present=True))
    import copy

    old_copy = copy.deepcopy(old)
    shadow_copy = copy.deepcopy(shadow)
    compute_calibration_metrics(old_rubric=old, shadow_rubric=shadow)
    assert old == old_copy
    assert shadow == shadow_copy
