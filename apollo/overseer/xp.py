"""XP formula + level tier table (Phase 2 gamification).

Pure-function, no DB, no LLM. Deterministic, auditable, reproducible.

XP formula:
    xp_earned = floor(max(0, overall_score) * difficulty_multiplier * reattempt_factor)

where difficulty_multiplier is a lookup by the three DB-canonical values
(intro / standard / hard) and reattempt_factor is 1.0 on first attempt,
REATTEMPT_MULTIPLIER (0.25) otherwise.

Levels are a 5-tier progression with cumulative XP thresholds drawn from
Section 5 of docs/superpowers/specs/2026-04-21-apollo-teaching-rigor-design.md."""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

DIFFICULTY_MULTIPLIERS: dict[str, float] = {
    "intro": 1.0,
    "standard": 1.5,
    "hard": 2.0,
}

REATTEMPT_MULTIPLIER: float = 0.25

# P3.2 decision D7 (2026-08-12 wrongness spec §8): fixing a misconception Apollo
# elicited is the one lever that CELEBRATES a mistake, and it is the counter to
# the pilot's "all-negative feedback" complaint. Flat, small, and additive.
MISCONCEPTION_CORRECTED_BONUS_XP: int = 10


@dataclass(frozen=True)
class LevelTier:
    level: int
    title: str
    threshold: int


# Ascending by threshold. `level_from_xp` scans in reverse for the
# highest tier whose threshold <= xp.
LEVEL_TIERS: list[LevelTier] = [
    LevelTier(level=1, title="Apollo Apprentice", threshold=0),
    LevelTier(level=2, title="Apollo Adept", threshold=300),
    LevelTier(level=3, title="Apollo Scholar", threshold=800),
    LevelTier(level=4, title="Apollo Sage", threshold=1600),
    LevelTier(level=5, title="Apollo Archon", threshold=3000),
]

_TIER_BY_LEVEL: dict[int, LevelTier] = {t.level: t for t in LEVEL_TIERS}
_MAX_LEVEL: int = max(_TIER_BY_LEVEL)


def compute_xp_earned(
    *,
    overall_score: int,
    difficulty: str,
    is_reattempt: bool,
) -> int:
    """Compute XP awarded for one Done event.

    Clamps negative scores to 0 (no negative XP, ever). Raises ValueError
    on unknown difficulty to surface upstream drift loudly rather than
    silently zero-award."""
    if difficulty not in DIFFICULTY_MULTIPLIERS:
        raise ValueError(
            f"unknown difficulty {difficulty!r}; "
            f"expected one of {sorted(DIFFICULTY_MULTIPLIERS)}"
        )
    base = max(0, int(overall_score))
    mult = DIFFICULTY_MULTIPLIERS[difficulty]
    raw = base * mult
    if is_reattempt:
        raw *= REATTEMPT_MULTIPLIER
    return int(math.floor(raw))


def compute_misconception_bonus(*, newly_resolved_keys: Sequence[str]) -> int:
    """Bonus XP for misconceptions the student RESOLVED this attempt (D7).

    Pure arithmetic: ``MISCONCEPTION_CORRECTED_BONUS_XP`` per DISTINCT key, and
    structurally never negative — XP only ever goes up (``progress_repo.
    apply_xp`` raises on a negative delta, and this bonus is additive on top of
    ``compute_xp_earned``, never a substitute for it).

    Every eligibility rule is the CALLER's job (``done.py`` at
    ``APOLLO_WRONGNESS_LEVEL >= 3``): the finding must be corroborated,
    Apollo-elicited (``last_asked_turn < correction_turn`` — a self-asserted,
    self-corrected claim is not farmable into XP), and not already awarded for
    this user x problem x node in an earlier attempt (see
    ``persistence.attempt_history.prior_wrongness_findings``). Passing keys that
    fail those rules is a caller bug, not something this function can detect —
    it only counts.
    """
    return MISCONCEPTION_CORRECTED_BONUS_XP * len(set(newly_resolved_keys))


def level_from_xp(xp: int) -> int:
    """Resolve the level number for a cumulative XP total."""
    if xp < 0:
        raise ValueError(f"xp must be non-negative; got {xp}")
    for tier in reversed(LEVEL_TIERS):
        if xp >= tier.threshold:
            return tier.level
    return 1  # Unreachable because tier[0].threshold == 0.


def title_for_level(level: int) -> str:
    """Return the cosmetic title for a level number (1..5)."""
    tier = _TIER_BY_LEVEL.get(level)
    if tier is None:
        raise ValueError(f"level {level} is out of range (1..{_MAX_LEVEL})")
    return tier.title


def next_tier_threshold(level: int) -> int | None:
    """Return the XP needed to reach the next tier, or None at max level."""
    if level not in _TIER_BY_LEVEL:
        raise ValueError(f"level {level} is out of range (1..{_MAX_LEVEL})")
    if level >= _MAX_LEVEL:
        return None
    return _TIER_BY_LEVEL[level + 1].threshold


@dataclass(frozen=True)
class ProgressEnvelope:
    """Item #9: backend-only source of truth for level / progress UI.

    Frontend renders these fields directly — no formula duplication. At
    max level, `xp_to_next_level` is None and `level_progress_pct` is
    100.0 (UI shows "MAX").
    """
    xp_earned: int
    xp_before: int
    xp_after: int
    level_before: int
    level_after: int
    level_up: bool
    title_after: str
    level_progress_pct: float
    xp_to_next_level: int | None


def compute_progress_envelope(
    *,
    xp_earned: int,
    xp_before: int,
    xp_after: int,
) -> ProgressEnvelope:
    """Build the full progress payload for the /done response.

    Derives level_before/level_after/level_up from the cumulative XPs and
    looks up the title + thresholds from LEVEL_TIERS. Pure function.
    """
    if xp_earned < 0:
        raise ValueError(f"xp_earned must be non-negative; got {xp_earned}")
    if xp_before < 0 or xp_after < 0:
        raise ValueError("xp_before and xp_after must be non-negative")
    if xp_after < xp_before:
        raise ValueError(
            f"xp_after ({xp_after}) cannot be lower than xp_before ({xp_before})"
        )

    level_before = level_from_xp(xp_before)
    level_after = level_from_xp(xp_after)
    level_up = level_after > level_before

    current_threshold = _TIER_BY_LEVEL[level_after].threshold
    next_threshold = next_tier_threshold(level_after)

    if next_threshold is None:
        # At max level — fully filled bar.
        progress_pct = 100.0
        xp_to_next = None
    else:
        span = next_threshold - current_threshold
        if span <= 0:
            progress_pct = 100.0
            xp_to_next = 0
        else:
            in_tier = max(0, xp_after - current_threshold)
            progress_pct = max(0.0, min(100.0, 100.0 * in_tier / span))
            xp_to_next = max(0, next_threshold - xp_after)

    return ProgressEnvelope(
        xp_earned=xp_earned,
        xp_before=xp_before,
        xp_after=xp_after,
        level_before=level_before,
        level_after=level_after,
        level_up=level_up,
        title_after=title_for_level(level_after),
        level_progress_pct=round(progress_pct, 2),
        xp_to_next_level=xp_to_next,
    )


__all__ = [
    "DIFFICULTY_MULTIPLIERS",
    "LEVEL_TIERS",
    "MISCONCEPTION_CORRECTED_BONUS_XP",
    "REATTEMPT_MULTIPLIER",
    "LevelTier",
    "ProgressEnvelope",
    "compute_misconception_bonus",
    "compute_progress_envelope",
    "compute_xp_earned",
    "level_from_xp",
    "next_tier_threshold",
    "title_for_level",
]
