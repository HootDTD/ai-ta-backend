---
doc: apollo/persistence/progress-repo
description: Course-scoped student-progress repository (XP + leveling) over app.student_progress.
owns:
  - apollo/persistence/progress_repo.py
related:
  - apollo/persistence/_index
  - apollo/persistence/models
  - apollo/persistence/done-write-linkage
  - apollo/overseer/xp
  - apollo/conversation/handlers/done
  - apollo/conversation/handlers/progress
last_verified: 2026-07-25
stub: false
---

# apollo/persistence/progress-repo

The per-course XP + leveling repository over `app.student_progress`.

## Interface

- **`load_progress(*, db, user_id, course_id) → StudentProgress`** — returns the
  per-course row, creating a default 0-XP / level-1 row if missing, then commits.
- **`apply_xp(*, db, user_id, course_id, xp_delta) → dict`** — adds the delta,
  recomputes the level via `overseer.xp.level_from_xp`, stamps `last_level_up_at`
  on a level change, commits, and returns
  `{xp_before, xp_after, level_before, level_after, level_up}` — the payload the
  Done response's `xp_earned` / `level_before` / `level_after` / `level_up` use.

## Data flow

`apollo/conversation/handlers/done` calls `apply_xp` at Done-time to award XP;
`apollo/conversation/handlers/progress` calls `load_progress` for the
`/apollo/progress` read; `apollo/projections/mastery` also reads progress.
`level_from_xp` comes from the overseer domain (`apollo/overseer/xp`).

## Invariants & gotchas

- **Both functions COMMIT internally** — callers must NOT wrap them in a nested
  transaction. `handle_done` commits the attempt + session phase separately.
- `xp_delta` must be **non-negative** (raises `ValueError` otherwise).
- XP/level are keyed per `(user_id, course_id)` (Invariant A1); the composite PK
  is on `StudentProgress`.

## Related

`apollo/persistence/models` (`StudentProgress` composite PK),
`apollo/persistence/done-write-linkage` (sibling Done-time helpers),
`apollo/overseer/xp` (`level_from_xp`).
