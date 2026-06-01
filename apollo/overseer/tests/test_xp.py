from __future__ import annotations

import math

import pytest

from apollo.overseer.xp import (
    DIFFICULTY_MULTIPLIERS,
    LEVEL_TIERS,
    REATTEMPT_MULTIPLIER,
    compute_progress_envelope,
    compute_xp_earned,
    level_from_xp,
    next_tier_threshold,
    title_for_level,
)


# ── Constants & tier table ────────────────────────────────────────────────

def test_difficulty_multipliers_cover_db_values():
    # Keys MUST match the three difficulty values in
    # database/migrations/009_apollo_slice0.sql's apollo_problem_attempts
    # CHECK constraint. Drift here produces silent zero-XP awards.
    assert set(DIFFICULTY_MULTIPLIERS) == {"intro", "standard", "hard"}
    assert DIFFICULTY_MULTIPLIERS["intro"] == 1.0
    assert DIFFICULTY_MULTIPLIERS["standard"] == 1.5
    assert DIFFICULTY_MULTIPLIERS["hard"] == 2.0


def test_reattempt_multiplier_is_one_quarter():
    assert REATTEMPT_MULTIPLIER == 0.25


def test_level_tiers_cover_five_levels_with_spec_thresholds():
    # Spec Section 5: [0, 300, 800, 1600, 3000] with titles.
    thresholds = [t.threshold for t in LEVEL_TIERS]
    assert thresholds == [0, 300, 800, 1600, 3000]
    titles = [t.title for t in LEVEL_TIERS]
    assert titles == [
        "Apollo Apprentice",
        "Apollo Adept",
        "Apollo Scholar",
        "Apollo Sage",
        "Apollo Archon",
    ]
    levels = [t.level for t in LEVEL_TIERS]
    assert levels == [1, 2, 3, 4, 5]


# ── compute_xp_earned ─────────────────────────────────────────────────────

def test_compute_xp_earned_intro_first_attempt():
    # A=90, intro×1.0, first attempt -> 90 XP.
    assert compute_xp_earned(overall_score=90, difficulty="intro", is_reattempt=False) == 90


def test_compute_xp_earned_standard_a_grade():
    # Spec example: A-grade Intermediate ≈ 135 XP. Our `standard` maps to 1.5.
    # 90 * 1.5 = 135.
    assert compute_xp_earned(overall_score=90, difficulty="standard", is_reattempt=False) == 135


def test_compute_xp_earned_hard_max_is_200():
    # Spec cap: max XP per session is 200 (Challenging A+).
    # Our `hard` multiplier is 2.0; 100 overall caps at 200.
    assert compute_xp_earned(overall_score=100, difficulty="hard", is_reattempt=False) == 200


def test_compute_xp_earned_reattempt_is_quarter():
    # 100 * 1.5 * 0.25 = 37.5 -> floor -> 37.
    assert compute_xp_earned(overall_score=100, difficulty="standard", is_reattempt=True) == 37


def test_compute_xp_earned_uses_floor_semantics():
    # 65 * 1.5 = 97.5 -> floor -> 97.
    assert compute_xp_earned(overall_score=65, difficulty="standard", is_reattempt=False) == 97


def test_compute_xp_earned_zero_grade_awards_zero():
    # Spec: No negative XP ever — a bad grade just earns less.
    assert compute_xp_earned(overall_score=0, difficulty="hard", is_reattempt=False) == 0


def test_compute_xp_earned_never_negative():
    # Even hostile inputs can't produce a negative XP value.
    assert compute_xp_earned(overall_score=-5, difficulty="intro", is_reattempt=False) == 0


def test_compute_xp_earned_rejects_unknown_difficulty():
    with pytest.raises(ValueError):
        compute_xp_earned(overall_score=80, difficulty="expert", is_reattempt=False)


# ── level_from_xp ─────────────────────────────────────────────────────────

def test_level_from_xp_boundary_values():
    assert level_from_xp(0) == 1
    assert level_from_xp(299) == 1
    assert level_from_xp(300) == 2
    assert level_from_xp(799) == 2
    assert level_from_xp(800) == 3
    assert level_from_xp(1599) == 3
    assert level_from_xp(1600) == 4
    assert level_from_xp(2999) == 4
    assert level_from_xp(3000) == 5
    assert level_from_xp(10_000) == 5


def test_level_from_xp_rejects_negative():
    with pytest.raises(ValueError):
        level_from_xp(-1)


# ── title_for_level / next_tier_threshold ─────────────────────────────────

def test_title_for_level_matches_tier_table():
    assert title_for_level(1) == "Apollo Apprentice"
    assert title_for_level(5) == "Apollo Archon"


def test_title_for_level_rejects_out_of_range():
    with pytest.raises(ValueError):
        title_for_level(0)
    with pytest.raises(ValueError):
        title_for_level(6)


def test_next_tier_threshold_returns_next_threshold_for_non_max_levels():
    assert next_tier_threshold(1) == 300
    assert next_tier_threshold(2) == 800
    assert next_tier_threshold(3) == 1600
    assert next_tier_threshold(4) == 3000


def test_next_tier_threshold_returns_none_at_max_level():
    assert next_tier_threshold(5) is None


def test_next_tier_threshold_rejects_out_of_range():
    with pytest.raises(ValueError):
        next_tier_threshold(0)
    with pytest.raises(ValueError):
        next_tier_threshold(6)


# ── Progress envelope (item #9) ────────────────────────────────────────────


def test_progress_envelope_basic_no_level_up():
    e = compute_progress_envelope(
        xp_earned=50, xp_before=100, xp_after=150,
    )
    assert e.xp_earned == 50
    assert e.xp_before == 100
    assert e.xp_after == 150
    assert e.level_before == 1
    assert e.level_after == 1
    assert e.level_up is False
    assert e.title_after == "Apollo Apprentice"
    # Level 1 spans 0..300 → 150 in tier → 50%
    assert e.level_progress_pct == 50.0
    assert e.xp_to_next_level == 150


def test_progress_envelope_signals_level_up():
    e = compute_progress_envelope(
        xp_earned=200, xp_before=200, xp_after=400,
    )
    assert e.level_before == 1
    assert e.level_after == 2
    assert e.level_up is True
    assert e.title_after == "Apollo Adept"
    # Level 2 spans 300..800 → 400 in tier (100/500)
    assert e.level_progress_pct == 20.0
    assert e.xp_to_next_level == 400


def test_progress_envelope_at_max_level():
    e = compute_progress_envelope(
        xp_earned=100, xp_before=3000, xp_after=3100,
    )
    assert e.level_before == 5
    assert e.level_after == 5
    assert e.level_up is False
    assert e.title_after == "Apollo Archon"
    assert e.level_progress_pct == 100.0
    assert e.xp_to_next_level is None


def test_progress_envelope_exact_threshold_boundary():
    """xp_after exactly hits a tier threshold => level up, 0% into new tier."""
    e = compute_progress_envelope(
        xp_earned=100, xp_before=200, xp_after=300,
    )
    assert e.level_after == 2
    assert e.level_up is True
    assert e.level_progress_pct == 0.0
    assert e.xp_to_next_level == 500  # 800 - 300


def test_progress_envelope_zero_xp_earned():
    """xp_earned=0 is valid (e.g. graded re-attempt with full credit at
    the cap). No level up when xp doesn't move."""
    e = compute_progress_envelope(
        xp_earned=0, xp_before=400, xp_after=400,
    )
    assert e.level_up is False
    assert e.xp_earned == 0


def test_progress_envelope_rejects_negative_xp_earned():
    with pytest.raises(ValueError):
        compute_progress_envelope(xp_earned=-1, xp_before=0, xp_after=0)


def test_progress_envelope_rejects_negative_before():
    with pytest.raises(ValueError):
        compute_progress_envelope(xp_earned=10, xp_before=-1, xp_after=10)


def test_progress_envelope_rejects_after_lower_than_before():
    with pytest.raises(ValueError):
        compute_progress_envelope(xp_earned=10, xp_before=100, xp_after=50)
