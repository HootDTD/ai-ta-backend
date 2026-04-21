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
from dataclasses import dataclass
from typing import Dict, List, Optional


DIFFICULTY_MULTIPLIERS: Dict[str, float] = {
    "intro": 1.0,
    "standard": 1.5,
    "hard": 2.0,
}

REATTEMPT_MULTIPLIER: float = 0.25


@dataclass(frozen=True)
class LevelTier:
    level: int
    title: str
    threshold: int


# Ascending by threshold. `level_from_xp` scans in reverse for the
# highest tier whose threshold <= xp.
LEVEL_TIERS: List[LevelTier] = [
    LevelTier(level=1, title="Apollo Apprentice", threshold=0),
    LevelTier(level=2, title="Apollo Adept", threshold=300),
    LevelTier(level=3, title="Apollo Scholar", threshold=800),
    LevelTier(level=4, title="Apollo Sage", threshold=1600),
    LevelTier(level=5, title="Apollo Archon", threshold=3000),
]

_TIER_BY_LEVEL: Dict[int, LevelTier] = {t.level: t for t in LEVEL_TIERS}
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


def next_tier_threshold(level: int) -> Optional[int]:
    """Return the XP needed to reach the next tier, or None at max level."""
    if level not in _TIER_BY_LEVEL:
        raise ValueError(f"level {level} is out of range (1..{_MAX_LEVEL})")
    if level >= _MAX_LEVEL:
        return None
    return _TIER_BY_LEVEL[level + 1].threshold
