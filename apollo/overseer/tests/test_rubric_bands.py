"""THE tripwire for the student-facing proficiency bands (study-prep §A.1/§A.5).

Two claims, pinned separately because they fail for different reasons.

**The boundary table** (§A.5): 0/29/30/49/50/84/85/100 map to the bands the spec
names. That is a claim about `score_to_band` alone.

**The LETTER_BANDS alignment** — the one that actually matters. `advanced >= 85`
and `intermediate >= 50` are only defensible because of what those numbers mean
on `rubric.LETTER_BANDS`: 85 is the A- floor, 50 the C floor, so the two
vocabularies change at the same score and can never be seen disagreeing (an 85
that reads "advanced" next to a B+ would be a bug report). The cuts are
deliberately NOT derived from `LETTER_BANDS` at runtime — a letter rescale must
FAIL here rather than silently drag the student-facing bands along with it. This
is the P3.2 ceiling pin (`test_ceiling_letter_bands.py`) applied to the band map:
the numbers and the map they depend on are pinned TOGETHER, and the relationship
is IMPORTED (`LETTER_BANDS` / `score_to_letter`), never re-declared.
"""

from __future__ import annotations

import pytest

from apollo.overseer.rubric import (
    BAND_TOKENS,
    LETTER_BANDS,
    PROFICIENCY_BANDS,
    PROFICIENCY_CUTS,
    band_from_served_overall,
    score_to_band,
    score_to_letter,
)
from apollo.overseer.topic_narrative import sanitize_narrative

pytestmark = pytest.mark.unit

_INTERMEDIATE_CUT, _ADVANCED_CUT = PROFICIENCY_CUTS

#: Spec §A.5's boundary table, verbatim.
_BOUNDARY_TABLE = [
    (0, "beginner"),
    (29, "beginner"),
    (30, "beginner"),
    (49, "beginner"),
    (50, "intermediate"),
    (84, "intermediate"),
    (85, "advanced"),
    (100, "advanced"),
]


# --------------------------------------------------------------------------- #
# The boundary table                                                           #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("score", "expected"), _BOUNDARY_TABLE)
def test_boundary_table(score, expected):
    assert score_to_band(score) == expected


def test_a_negative_score_still_lands_in_a_band():
    """Defensive tail of `score_to_band`: no caller should ever pass one, but a
    band map that can return None would push the fiction onto the wire."""
    assert score_to_band(-1) == "beginner"


def test_band_is_monotone_over_the_whole_scale():
    """Ranked, non-decreasing: a higher score can never buy a lower band."""
    rank = {"beginner": 0, "intermediate": 1, "advanced": 2}
    ranks = [rank[score_to_band(score)] for score in range(0, 101)]
    assert ranks == sorted(ranks)


def test_the_wire_vocabulary_is_exactly_three_lowercase_tokens():
    """Display strings ("Beginner", ...) belong to the UI. A capitalized token
    on the wire would make the client's map a second source of truth."""
    assert BAND_TOKENS == {"beginner", "intermediate", "advanced"}
    assert {score_to_band(score) for score in range(0, 101)} == BAND_TOKENS
    assert all(token == token.lower() for token in BAND_TOKENS)
    assert [band for _threshold, band in PROFICIENCY_BANDS] == [
        "advanced",
        "intermediate",
        "beginner",
    ]


# --------------------------------------------------------------------------- #
# The LETTER_BANDS alignment pin                                               #
# --------------------------------------------------------------------------- #


def test_the_cuts_sit_on_letter_floors():
    """85 is the A- floor and 50 the C floor TODAY. If this fails, the letter
    map moved under the bands — re-derive the cuts from the §A.4 lattice
    analysis, do NOT edit these numbers to match."""
    assert PROFICIENCY_CUTS == (50, 85)
    assert (85, "A-") in LETTER_BANDS
    assert (50, "C") in LETTER_BANDS
    assert score_to_letter(_ADVANCED_CUT) == "A-"
    assert score_to_letter(_INTERMEDIATE_CUT) == "C"


def test_each_cut_is_a_floor_not_a_point_inside_a_letter():
    """The score one below each cut must be a DIFFERENT letter, so no cut ever
    straddles a letter band (which is how an 85 could read "advanced" while the
    84 beside it reads the same letter)."""
    assert score_to_letter(_ADVANCED_CUT - 1) != score_to_letter(_ADVANCED_CUT)
    assert score_to_letter(_INTERMEDIATE_CUT - 1) != score_to_letter(_INTERMEDIATE_CUT)


def test_every_band_boundary_is_also_a_letter_boundary():
    """The relationship itself, over the whole scale rather than at two points:
    the set of scores where the BAND changes must be a subset of the set where
    the LETTER changes, and must be exactly the declared cuts. A letter rescale
    that moved 50 or 85 fails this even if it kept some other letter at that
    threshold."""
    letter_edges = {s for s in range(1, 101) if score_to_letter(s) != score_to_letter(s - 1)}
    band_edges = {s for s in range(1, 101) if score_to_band(s) != score_to_band(s - 1)}

    assert band_edges == set(PROFICIENCY_CUTS)
    assert band_edges <= letter_edges


def test_the_dark_ceiling_is_not_advanced():
    """P3.2's `CEILING_UNCORRECTED = 84` is a B+, one point under the advanced
    cut. Pinned here too: a ceilinged student must never be shown the top band,
    which is the whole point of ceilinging them."""
    from apollo.overseer.topic_score import CEILING_UNCORRECTED

    assert score_to_band(CEILING_UNCORRECTED) == "intermediate"


def test_no_payload_module_redeclares_a_band_token():
    """One owner for the band map (the P3.2 seam-S7 pattern). The serving
    modules import `score_to_band` / `band_from_served_overall`; a literal band
    string in any of them would be a second, silently-diverging cut table."""
    from apollo.handlers import browse, done, progress

    for module in (done, browse, progress):
        source = (module.__file__ or "").strip()
        with open(source, encoding="utf-8") as handle:
            code = [line for line in handle if not line.lstrip().startswith("#")]
        for token in BAND_TOKENS:
            assert not [line for line in code if f'"{token}"' in line], (source, token)


# --------------------------------------------------------------------------- #
# `band_from_served_overall` — the re-serving resolution rule                  #
# --------------------------------------------------------------------------- #


def test_a_persisted_band_wins_over_re_derivation():
    """Snapshot first: browse/progress re-serve what the student SAW, so a later
    cut move must not relabel an attempt retroactively."""
    assert band_from_served_overall({"score": 90, "band": "beginner"}) == "beginner"


def test_a_row_without_a_band_falls_back_to_its_own_score():
    """Rows graded before the key existed have no served band to preserve, so
    the fallback derives from the snapshot's own (verbatim) score."""
    assert band_from_served_overall({"score": 90}) == "advanced"
    assert band_from_served_overall({"score": 60.4}) == "intermediate"


def test_a_band_outside_the_wire_vocabulary_is_not_a_snapshot():
    """Corruption (or a client-invented token) must not be served straight
    through — it falls back to the score like any other bandless row."""
    assert band_from_served_overall({"score": 90, "band": "Advanced"}) == "advanced"
    assert band_from_served_overall({"score": 10, "band": 3}) == "beginner"


@pytest.mark.parametrize("overall", [{}, {"score": None}, {"score": "90"}, {"score": True}])
def test_no_usable_score_yields_no_band(overall):
    """`None`, not a guessed band: these are exactly the rows where `letter` is
    already None on the re-serving surfaces, so the two keys agree about whether
    there is a grade to show."""
    assert band_from_served_overall(overall) is None


# --------------------------------------------------------------------------- #
# The narrative scrubber must not eat the band vocabulary (§A brief item 4)    #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("token", sorted(BAND_TOKENS))
def test_sanitize_narrative_leaves_band_words_intact(token):
    """Proved, not assumed. `_SCORING_TERM` only matches credit/weight/dock
    followed by a 0-1 decimal, but the band words now reach student-facing prose
    and the paren rule deletes WHOLE parentheticals — so both shapes are tested.
    """
    for text in (
        f"You are at the {token} level on this topic.",
        f"Nice work ({token} on continuity) — keep going.",
        f"{token.capitalize()}: you explained the pressure term clearly.",
    ):
        assert sanitize_narrative(text) == text


def test_the_scrubber_can_still_fire():
    """Control: without this, the test above would pass on a scrubber that had
    been accidentally turned into the identity function."""
    assert "credit" not in sanitize_narrative("Good work (credit 0.80) on continuity.")
