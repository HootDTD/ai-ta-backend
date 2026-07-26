---
doc: apollo/conversation/handlers/progress
description: apollo/handlers/progress.py — the course-scoped XP/level read surface
owns:
  - apollo/handlers/progress.py
related:
  - apollo/conversation/routing/router
  - apollo/persistence/progress-repo
  - apollo/overseer/xp
last_verified: 2026-07-26
stub: false
---

# handlers/progress — course-scoped XP/level

`apollo/handlers/progress.py` is the read surface for a student's Apollo
progress within one course.

## Interface

- `handle_get_progress_detail(*, db, user_id, search_space_id) -> dict` — the
  public route (via `routing/router` GET `/apollo/progress`). Returns base XP /
  level / title + a `detail` block: per-concept mastery (averaged over
  `apollo_learner_state`) and the 10 most-recent GRADED attempts.
- `handle_get_progress(*, db, user_id, search_space_id) -> dict` — internal base
  helper (XP/level lookup via `load_progress`, `persistence/progress-repo`); NOT
  a route.

## Data flow

`handle_get_progress_detail` composes the base helper, then reads mastery rows
(`LearnerState`→`LearnerEntity`→`Concept`) and recent graded attempts
(`ProblemAttempt.diagnostic_report`, filtered to `result == "graded"`). Each
attempt's score/letter prefers the report's `served_overall` snapshot (the
grade the student was shown — see `handlers/done`) and falls back to the raw
`rubric.overall` for rows graded before the snapshot existed. Level
title/threshold come from `overseer/xp` (`title_for_level`, `next_tier_threshold`).

## Invariants & gotchas

- **Invariant A1: XP and level are PER COURSE** — `app.student_progress` is keyed
  by `(user_id, course_id)`; the response echoes `search_space_id` so clients
  cannot confuse two enrolled courses.
- Course membership + session ownership are enforced upstream (`routing/auth-deps`).
- The recent-attempts query deliberately uses the narrow `"graded"` literal — do
  not widen it, or null-score legacy rows pollute the dashboard.

## Related

Route wiring: `routing/router`; XP/level store: `persistence/progress-repo`;
tier tables: `overseer/xp`.
