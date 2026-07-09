"""WU-3C2 Step 6 — pure-unit tests for misconception competition + polarity screen.

No Docker, no network. Pin (§5 anti-over-normalization guardrails / §6.11):
- a polar near-miss ("pressure increases with speed") out-competes the
  lexically-close reference/definition and resolves to the misconception;
- the polarity screen rejects a direction-inverted fuzzy match against a
  non-misconception candidate.
"""

from __future__ import annotations

import pytest

from apollo.resolution.candidates import Candidate
from apollo.resolution.competition import (
    apply_misconception_competition,
    polarity_screen,
)
from apollo.resolution.structural import ScoredMatch


def _cand(key, *, is_misc=False, aliases=()):
    return Candidate(
        canonical_key=key,
        canon_key=1,
        node_type="definition",
        is_misconception=is_misc,
        symbolic=None,
        aliases=tuple(aliases),
        display_name=key,
        opposes_key="def.pressure_velocity_tradeoff" if is_misc else None,
    )


def test_polar_near_miss_resolves_to_misconception_not_reference():
    """'pressure increases with speed' competes against both the tradeoff
    definition and the same-direction misconception; the misconception wins
    because the wrong claim is out-competed, not merely thresholded.

    The scores here are RAW lexical proximities (what the wired path now feeds —
    Defect A): the reference is the lexically CLOSER match (higher raw score),
    yet the misconception within the 0.05 margin still wins."""
    definition = _cand("def.pressure_velocity_tradeoff")
    misconception = _cand(
        "misc.pressure_velocity_same_direction",
        is_misc=True,
        aliases=("faster flow means higher pressure",),
    )
    student_text = "pressure increases with speed"
    candidate_matches = [
        ScoredMatch("s1", definition, method="fuzzy", score=0.93),  # raw: closer
        ScoredMatch("s1", misconception, method="fuzzy", score=0.90),  # within margin
    ]
    winner = apply_misconception_competition(student_text, candidate_matches)
    assert winner is not None
    assert winner.candidate.canonical_key == "misc.pressure_velocity_same_direction"


def test_polarity_screen_rejects_direction_inverted_fuzzy():
    """A high-fuzzy candidate whose direction word is inverted relative to the
    student text is screened out (rejected) for a non-misconception target."""
    # student says pressure goes UP; alias says pressure goes DOWN -> inverted.
    assert (
        polarity_screen(
            "pressure goes up when speed rises",
            "pressure goes down when speed rises",
        )
        is False
    )
    # same direction -> passes.
    assert (
        polarity_screen(
            "pressure drops when speed rises",
            "pressure goes down when speed rises",
        )
        is True
    )


def test_competition_returns_best_non_misconception_when_no_misc_present():
    """With no misconception in the running, the highest-scoring match wins
    normally (competition only re-ranks when a misconception is present)."""
    a = _cand("def.a")
    b = _cand("def.b")
    matches = [
        ScoredMatch("s1", a, method="fuzzy", score=0.91),
        ScoredMatch("s1", b, method="fuzzy", score=0.95),
    ]
    winner = apply_misconception_competition("some neutral claim", matches)
    assert winner is not None
    assert winner.candidate.canonical_key == "def.b"


def test_competition_empty_matches_returns_none():
    assert apply_misconception_competition("anything", []) is None


def test_competition_misconception_loses_when_far_below_margin():
    """A misconception scoring well BELOW the best non-misconception (beyond the
    margin) does NOT win — the best overall is returned."""
    definition = _cand("def.tradeoff")
    misconception = _cand("misc.x", is_misc=True)
    matches = [
        ScoredMatch("s1", definition, method="alias", score=0.95),
        ScoredMatch("s1", misconception, method="fuzzy", score=0.50),  # far below
    ]
    winner = apply_misconception_competition("a clearly-correct claim", matches)
    assert winner is not None
    assert winner.candidate.canonical_key == "def.tradeoff"


# --- Macro polarity antonyms (DESIGN §"Polarity antonyms") -------------------
#
# Each new macroeconomics pair must INVERT a fuzzy lexical match (one text uses
# the left word, the other the right -> screened out), while a word shared by
# BOTH texts is non-discriminating and must NOT reject the match.

# (left, right, neutral-shared-word) per appended macro pair.
_MACRO_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("surplus", "deficit", "trade"),
    ("rises", "falls", "output"),
    ("rise", "fall", "prices"),
    ("appreciate", "depreciate", "currency"),
    ("appreciates", "depreciates", "currency"),
    ("expansionary", "contractionary", "policy"),
    ("inflation", "deflation", "expected"),
    ("gross", "net", "product"),
    ("nominal", "real", "gdp"),
    ("multiply", "divide", "deflator"),
)


@pytest.mark.parametrize("left,right,shared", _MACRO_PAIRS)
def test_macro_antonym_inverts_lexical_match(left: str, right: str, shared: str):
    """Each macro pair: a text asserting LEFT vs one asserting RIGHT is polar
    inverted, so the polarity screen rejects the lexical match (returns False),
    in BOTH word orders."""
    student_left = f"the {shared} {left}"
    candidate_right = f"the {shared} {right}"
    assert polarity_screen(student_left, candidate_right) is False
    # Symmetric: order of the pair across the two texts must not matter.
    assert polarity_screen(candidate_right, student_left) is False


@pytest.mark.parametrize("left,right,shared", _MACRO_PAIRS)
def test_macro_antonym_shared_word_does_not_discriminate(
    left: str, right: str, shared: str
):
    """A direction word present in BOTH texts is not discriminating between the
    two phrases — the pair is skipped and the (otherwise neutral) match passes."""
    # `left` appears in both texts -> the pair is skipped, screen passes.
    assert polarity_screen(f"{shared} {left} sharply", f"{shared} {left} a bit") is True
    # Same for the right word shared across both texts.
    assert polarity_screen(f"{shared} {right} sharply", f"{shared} {right} a bit") is True


def test_macro_antonym_neutral_text_passes():
    """Macro-domain text with no direction word at all always passes (neutral)."""
    assert polarity_screen("the gdp identity sums four components", "gdp = c + i + g + nx") is True
