"""THE band tripwire for the dark level-4 ceiling (P3.2 §2.3 L4, §4 testing
contract).

``CEILING_UNCORRECTED = 84`` is only a defensible consequence because of what
it means on ``rubric.LETTER_BANDS``: a B+. P-1 ("a `min()` can never produce a D
or an F") is a claim about the BANDS, not about the number, so the number and
the band map must be pinned TOGETHER. `topic_score` imports `score_to_letter`
and never re-declares a band, and these tests fail loudly if the bands are ever
rescaled under the ceiling — which would silently turn 84 into a different
grade for every ceilinged student.
"""

from __future__ import annotations

import pytest

from apollo.overseer.rubric import LETTER_BANDS, score_to_letter
from apollo.overseer.topic_score import CEILING_UNCORRECTED

pytestmark = pytest.mark.unit


def test_ceiling_sits_at_b_plus():
    """80 <= 84 < 85 on today's bands. If this fails, the bands moved under the
    ceiling — re-derive the ceiling from the spec, do NOT edit this number."""
    assert CEILING_UNCORRECTED == 84
    assert score_to_letter(CEILING_UNCORRECTED) == "B+"
    assert (80, "B+") in LETTER_BANDS
    assert (85, "A-") in LETTER_BANDS


def test_ceiling_is_inside_the_band_it_claims():
    """The whole [ceiling, next band) window must be B+, so a one-point band
    rescale cannot leave the ceiling straddling two letters."""
    assert {score_to_letter(score) for score in range(CEILING_UNCORRECTED, 85)} == {"B+"}


def test_p1_holds_over_every_band_boundary():
    """P-1 restated over the band map itself: ceilinging never moves a score
    into a WORSE band than it was in, and never into D or F from above."""
    for raw in range(0, 101):
        served = min(raw, CEILING_UNCORRECTED)
        assert served <= raw
        if score_to_letter(raw) not in {"D", "F"}:
            assert score_to_letter(served) not in {"D", "F"}


def test_topic_score_does_not_redeclare_a_letter_band():
    """`score_to_letter` is imported, never re-declared (seam S7) — one owner
    for the band map."""
    from apollo.overseer import topic_score

    source = (topic_score.__file__ or "").strip()
    assert source.endswith("topic_score.py")
    with open(source, encoding="utf-8") as handle:
        code = [line for line in handle if not line.lstrip().startswith("#")]
    assert any("from apollo.overseer.rubric import score_to_letter" in line for line in code)
    # No assignment and no import of the band map — only prose may name it.
    assert not [line for line in code if "LETTER_BANDS" in line]
    assert not hasattr(topic_score, "LETTER_BANDS")
