---
doc: apollo/overseer/xp
description: Pure XP formula, 5-tier level table, and the progress envelope for the Done response.
owns:
  - apollo/overseer/xp.py
related:
  - apollo/overseer/problem-selector
  - apollo/conversation/handlers/done
last_verified: 2026-08-12
stub: false
---

# Overseer XP — formula + level tiers

Phase-2 gamification: pure, deterministic, no DB/LLM.

## Interface

- `compute_xp_earned(*, overall_score, difficulty, is_reattempt) -> int`.
- `compute_misconception_bonus(*, newly_resolved_keys) -> int` (2026-08-12 P3.2
  decision D7) — `MISCONCEPTION_CORRECTED_BONUS_XP` (10) per DISTINCT key, pure
  arithmetic, structurally never negative.
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
- **XP only ever goes UP (P3.2 D7).** `compute_misconception_bonus` is ADDITIVE
  on top of `compute_xp_earned` — never a substitute, never a dock — which is
  what keeps `persistence/progress_repo.apply_xp`'s "raises on a negative
  delta" guard un-trippable. This module does arithmetic only: **every**
  eligibility rule is the caller's (`handlers/done.py` at
  `APOLLO_WRONGNESS_LEVEL >= 3`) — the finding must be **`resolved`** (never
  `corroborated`; S2′ makes those two mutually exclusive, so a `corroborated`
  bonus population is always empty), Apollo-elicited (`last_asked_turn is not
  None` — see [wrongness](wrongness.md) for why the spec's
  `< correction_turn` is a presence test in code), and not already awarded for this
  user × problem × node in an earlier attempt (`persistence/attempt_history.
  prior_wrongness_findings`). Dedup of DISTINCT keys inside one call is the
  function's only guard. The bonus is DARK until that caller ships: nothing
  calls it at `APOLLO_WRONGNESS_LEVEL = 0`.
