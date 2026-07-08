"""RED->GREEN tests for ``apollo.overseer.misconception_detector.apply``.

Covers T8 (A4, A8):
  - ``apply_penalty`` subtracts the merge penalty, clamps to [0, 1], and
    rounds via the inline ``round(max(0.0, min(1.0, x)), 6)`` convention
    (A8 — no cross-package import of ``_round_like_composite``).
  - ``ceiling_applied`` caps the result at (or below) ``CEILING_COMPOSITE``
    (the SCORECARD/named-band ceiling, A4) even when the raw subtraction
    would have left the composite higher.
  - An empty ``MergeOutcome`` (penalty 0.0, ceiling not applied) returns the
    input composite unchanged (mod rounding).
  - ``rubric_overall_after_penalty`` returns a NEW dict (the original input
    is left unmutated), reduces ``overall.score`` (int 0-100) by the penalty
    scaled to the 0-100 range, and recomputes ``overall.letter`` via
    ``rubric.py::score_to_letter`` on the NEW score (A4 — letter bands, not
    named bands; no ``CEILING_COMPOSITE`` applied on the rubric side).
"""

from __future__ import annotations

import copy

import pytest

from apollo.overseer.misconception_detector.apply import (
    apply_penalty,
    rubric_overall_after_penalty,
)
from apollo.overseer.misconception_detector.config import CEILING_COMPOSITE
from apollo.overseer.misconception_detector.types import MergeOutcome
from apollo.overseer.rubric import score_to_letter


def _outcome(
    *,
    penalty: float = 0.0,
    ceiling_applied: bool = False,
    misconceptions: tuple[dict, ...] = (),
) -> MergeOutcome:
    return MergeOutcome(
        misconception_penalty=penalty,
        misconceptions=misconceptions,
        ceiling_applied=ceiling_applied,
        ledger_findings=(),
    )


EMPTY_OUTCOME = _outcome()


class TestApplyPenaltyComposite:
    def test_subtracts_penalty_and_renorms(self) -> None:
        outcome = _outcome(penalty=0.10)
        result = apply_penalty(composite=0.90, outcome=outcome)
        assert result == round(max(0.0, min(1.0, 0.90 - 0.10)), 6)

    def test_clamps_to_zero_floor(self) -> None:
        outcome = _outcome(penalty=0.30)
        result = apply_penalty(composite=0.05, outcome=outcome)
        assert result == 0.0

    def test_clamps_to_one_ceiling_when_no_penalty_and_over_one(self) -> None:
        # Defensive: even a pathological composite > 1.0 clamps.
        outcome = _outcome(penalty=0.0)
        result = apply_penalty(composite=1.5, outcome=outcome)
        assert result == 1.0

    def test_rounding_matches_inline_composite_convention(self) -> None:
        outcome = _outcome(penalty=0.123456789)
        result = apply_penalty(composite=0.987654321, outcome=outcome)
        expected = round(max(0.0, min(1.0, 0.987654321 - 0.123456789)), 6)
        assert result == expected
        # 6 decimal places, never more.
        assert result == round(result, 6)

    def test_empty_outcome_returns_composite_unchanged(self) -> None:
        result = apply_penalty(composite=0.9123, outcome=EMPTY_OUTCOME)
        assert result == round(max(0.0, min(1.0, 0.9123)), 6)

    def test_ceiling_applied_caps_below_named_band(self) -> None:
        # Even a tiny penalty + ceiling_applied must cap at CEILING_COMPOSITE
        # when the raw subtraction would otherwise leave it >= ceiling.
        outcome = _outcome(penalty=0.01, ceiling_applied=True)
        result = apply_penalty(composite=0.99, outcome=outcome)
        assert result <= CEILING_COMPOSITE
        assert result == round(max(0.0, min(1.0, CEILING_COMPOSITE)), 6)

    def test_ceiling_not_applied_allows_high_composite(self) -> None:
        outcome = _outcome(penalty=0.0, ceiling_applied=False)
        result = apply_penalty(composite=0.99, outcome=outcome)
        assert result == 0.99

    def test_ceiling_does_not_raise_an_already_lower_composite(self) -> None:
        # If subtraction already pushed the composite below the ceiling,
        # applying the ceiling must not raise it back up.
        outcome = _outcome(penalty=0.50, ceiling_applied=True)
        result = apply_penalty(composite=0.60, outcome=outcome)
        assert result == round(max(0.0, min(1.0, 0.60 - 0.50)), 6)

    def test_custom_ceiling_argument_respected(self) -> None:
        outcome = _outcome(penalty=0.0, ceiling_applied=True)
        result = apply_penalty(composite=0.95, outcome=outcome, ceiling=0.5)
        assert result == 0.5


class TestRubricOverallAfterPenalty:
    def _rubric(self) -> dict:
        return {
            "overall": {"score": 90, "letter": "A"},
            "procedure": {"score": 90, "letter": "A", "present": True},
            "justification": {"score": 90, "letter": "A", "present": True},
            "simplification": {"score": 90, "letter": "A", "present": True},
            "misconception_corrected": {
                "score": 0,
                "letter": "F",
                "present": False,
                "detected": 0,
                "resolved": 0,
            },
        }

    def test_returns_new_dict_original_unmutated(self) -> None:
        rubric = self._rubric()
        original = copy.deepcopy(rubric)
        outcome = _outcome(penalty=0.20)

        result = rubric_overall_after_penalty(rubric, outcome)

        assert rubric == original
        assert result is not rubric
        assert result["overall"] is not rubric["overall"]

    def test_reduces_overall_score_and_recomputes_letter(self) -> None:
        rubric = self._rubric()
        outcome = _outcome(penalty=0.20)

        result = rubric_overall_after_penalty(rubric, outcome)

        expected_score = max(0, min(100, 90 - round(0.20 * 100)))
        assert result["overall"]["score"] == expected_score
        assert result["overall"]["letter"] == score_to_letter(expected_score)

    def test_score_never_drops_below_zero(self) -> None:
        rubric = self._rubric()
        rubric["overall"]["score"] = 10
        outcome = _outcome(penalty=0.99)

        result = rubric_overall_after_penalty(rubric, outcome)

        assert result["overall"]["score"] == 0
        assert result["overall"]["letter"] == score_to_letter(0)

    def test_empty_outcome_leaves_overall_unchanged_value(self) -> None:
        rubric = self._rubric()

        result = rubric_overall_after_penalty(rubric, EMPTY_OUTCOME)

        assert result["overall"]["score"] == rubric["overall"]["score"]
        assert result["overall"]["letter"] == rubric["overall"]["letter"]
        assert result is not rubric

    def test_other_axes_preserved_unmodified(self) -> None:
        rubric = self._rubric()
        outcome = _outcome(penalty=0.15)

        result = rubric_overall_after_penalty(rubric, outcome)

        assert result["procedure"] == rubric["procedure"]
        assert result["justification"] == rubric["justification"]
        assert result["simplification"] == rubric["simplification"]
        assert result["misconception_corrected"] == rubric["misconception_corrected"]

    def test_does_not_apply_named_band_ceiling(self) -> None:
        # A4: rubric side has NO CEILING_COMPOSITE / named-band concept.
        # A large penalty with ceiling_applied=True still only reduces the
        # score arithmetically -- it must not clamp to some letter tied to
        # CEILING_COMPOSITE (that ceiling belongs to apply_penalty only).
        rubric = self._rubric()
        rubric["overall"]["score"] = 100
        outcome = _outcome(penalty=0.01, ceiling_applied=True)

        result = rubric_overall_after_penalty(rubric, outcome)

        # 0.01 * 100 = 1 point off -> 99, letter A+ still possible (>=97).
        assert result["overall"]["score"] == 99
        assert result["overall"]["letter"] == score_to_letter(99)
