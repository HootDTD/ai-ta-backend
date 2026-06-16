"""WU-3C2 Step 6 — pure-unit tests for misconception competition + polarity screen.

No Docker, no network. Pin (§5 anti-over-normalization guardrails / §6.11):
- a polar near-miss ("pressure increases with speed") out-competes the
  lexically-close reference/definition and resolves to the misconception;
- the polarity screen rejects a direction-inverted fuzzy match against a
  non-misconception candidate.
"""

from __future__ import annotations

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
    because the wrong claim is out-competed, not merely thresholded."""
    definition = _cand("def.pressure_velocity_tradeoff")
    misconception = _cand(
        "misc.pressure_velocity_same_direction",
        is_misc=True,
        aliases=("faster flow means higher pressure",),
    )
    student_text = "pressure increases with speed"
    candidate_matches = [
        ScoredMatch("s1", definition, method="fuzzy", score=0.91),
        ScoredMatch("s1", misconception, method="fuzzy", score=0.93),
    ]
    winner = apply_misconception_competition(student_text, candidate_matches)
    assert winner is not None
    assert winner.candidate.canonical_key == "misc.pressure_velocity_same_direction"


def test_polarity_screen_rejects_direction_inverted_fuzzy():
    """A high-fuzzy candidate whose direction word is inverted relative to the
    student text is screened out (rejected) for a non-misconception target."""
    # student says pressure goes UP; alias says pressure goes DOWN -> inverted.
    assert polarity_screen(
        "pressure goes up when speed rises",
        "pressure goes down when speed rises",
    ) is False
    # same direction -> passes.
    assert polarity_screen(
        "pressure drops when speed rises",
        "pressure goes down when speed rises",
    ) is True


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
