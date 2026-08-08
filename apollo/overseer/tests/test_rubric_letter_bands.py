"""Letter-band rescale (2026-08-07 bimodal-fix P1.5 / decision D1).

The pre-fix map gave F half the numeric scale (``F = [0, 50)``), so with the
de-facto binary per-node credit and 1-3 graded nodes per problem the reachable
score set was ~{0, 33, 50, 67, 100} -> letters {F, F, D, C+, A+} and a "B" was
arithmetically unreachable on most problems. D1 rescales the BOTTOM of the map
only:

* ``F  = [0, 30)``  — reserved for attempts that demonstrated ~nothing
* ``D  = [30, 50)`` — "missed most of it", no longer read as total failure
* ``C  = [50, 65)`` — the C range now starts at 50 (was 60)
* every A/B threshold and the ``C+`` threshold are UNCHANGED, and the letter
  SET is unchanged (no new band was introduced — ``letter_distribution`` in
  ``projections/performance_problems`` renders one bucket per band, so a new
  letter would have been a cross-repo teacher-UI surface change).

``apollo/overseer/tests/test_rubric.py`` is skipped at module level (legacy V2
signatures), so these band assertions live in their own live module.
"""

from __future__ import annotations

import pytest

from apollo.overseer.rubric import LETTER_BANDS, score_to_letter

pytestmark = pytest.mark.unit


def test_every_score_maps_to_a_letter_and_the_letter_set_is_unchanged():
    letters = {score_to_letter(score) for score in range(0, 101)}
    assert letters == {"A+", "A", "A-", "B+", "B", "B-", "C+", "C", "D", "F"}


def test_bands_are_descending_and_start_at_zero():
    thresholds = [threshold for threshold, _letter in LETTER_BANDS]
    assert thresholds == sorted(thresholds, reverse=True)
    assert LETTER_BANDS[0] == (97, "A+")
    assert LETTER_BANDS[-1] == (0, "F")


@pytest.mark.parametrize(
    ("score", "letter"),
    [
        (0, "F"),
        (29, "F"),
        (30, "D"),
        (49, "D"),
        (50, "C"),
        (64, "C"),
        (65, "C+"),
        (69, "C+"),
        (70, "B-"),
        (75, "B"),
        (80, "B+"),
        (85, "A-"),
        (90, "A"),
        (97, "A+"),
        (100, "A+"),
    ],
)
def test_rescaled_band_boundaries(score: int, letter: str):
    assert score_to_letter(score) == letter


def test_f_band_is_no_longer_half_the_scale():
    """The defect this fixes: 'missed one of two graded nodes' scored 50 and
    read as an F. It now reads as a C, and only sub-30 work is an F."""
    assert score_to_letter(50) == "C"
    assert score_to_letter(33) == "D"
    assert score_to_letter(25) == "F"


def test_a_and_b_thresholds_are_untouched_by_the_rescale():
    frozen = [(97, "A+"), (90, "A"), (85, "A-"), (80, "B+"), (75, "B"), (70, "B-"), (65, "C+")]
    assert LETTER_BANDS[: len(frozen)] == frozen


def test_score_below_the_lowest_band_still_degrades_to_f():
    """Defensive tail of ``score_to_letter`` — a negative score can only arrive
    from a corrupt caller, and must never raise."""
    assert score_to_letter(-1) == "F"
