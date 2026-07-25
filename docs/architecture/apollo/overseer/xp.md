---
doc: apollo/overseer/xp
description: Pure XP formula, 5-tier level table, and the progress envelope for the Done response.
owns:
  - apollo/overseer/xp.py
related:
  - apollo/overseer/problem-selector
  - apollo/conversation/handlers/done
last_verified: 2026-07-25
stub: false
---

# Overseer XP — formula + level tiers

Phase-2 gamification: pure, deterministic, no DB/LLM.

## Interface

- `compute_xp_earned(*, overall_score, difficulty, is_reattempt) -> int`.
- `level_from_xp`, `title_for_level`, `next_tier_threshold` — walk `LEVEL_TIERS`.
- `compute_progress_envelope(*, xp_earned, xp_before, xp_after) ->
  ProgressEnvelope`.
- `ProgressEnvelope`, `LevelTier` value objects.

Imported by `handlers/done.py`, `handlers/progress.py`, and
`persistence/progress_repo.py`.

## Data flow

`done.py` computes `xp_earned = compute_xp_earned(overall_score=
served_rubric["overall"]["score"], ...)` — so XP is earned against the served
**topic score**, not the axis blend (this call must stay after the
`served_rubric` swap). After `apply_xp`, `compute_progress_envelope` builds the
level/threshold payload the FE renders directly.

## Invariants & gotchas

- **Formula:** `floor(max(0, overall_score) × difficulty_multiplier ×
  reattempt_factor)`. `DIFFICULTY_MULTIPLIERS` = intro 1.0 / standard 1.5 /
  hard 2.0; `REATTEMPT_MULTIPLIER` = 0.25 (first attempt = 1.0).
- **Fail-loud:** an unknown `difficulty` raises `ValueError` rather than silently
  zero-awarding; negative scores clamp to 0 (never negative XP).
- **5 tiers** (Apprentice→Archon) with cumulative thresholds 0/300/800/1600/3000;
  at max level `xp_to_next_level` is `None` and `level_progress_pct` is 100.0.
- XP/level are per `(user_id, course_id)` — the caller supplies course scope.
