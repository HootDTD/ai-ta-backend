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
last_verified: 2026-08-11
stub: false
---

# apollo/persistence/progress-repo

The per-course XP + leveling repository over `app.student_progress`.

## Interface

- **`load_progress(*, db, user_id, course_id) → StudentProgress`** — materializes
  the per-course row with `INSERT … ON CONFLICT (user_id, course_id) DO NOTHING`
  (never check-then-insert), commits, and re-reads it with `populate_existing`.
- **`apply_xp(*, db, user_id, course_id, xp_delta) → dict`** — ATOMIC:
  `UPDATE … SET xp_total = xp_total + :delta … RETURNING xp_total`. Every
  returned field is derived from that returned total, the level is written back
  under a `level < :level_after` ratchet, `last_level_up_at` is stamped on a
  level change, then it commits and returns
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
- **M2 (P3.4): no read-modify-write anywhere.** `student_progress` has no
  per-award row and no idempotency key, and it commits BEFORE the artifact's
  `UNIQUE(attempt_id, role, grader_version)` fires, so that constraint cannot
  shield it — the atomicity has to live here. Never reintroduce
  `row.xp_total = row.xp_total + delta`.
- **`_insert_for` dispatches the dialect** so the ON CONFLICT path is exercised
  by BOTH the SQLite unit suite and the real-Postgres gate
  (`tests/database/test_apollo_progress_repo_concurrency_postgres.py`).

## Related

`apollo/persistence/models` (`StudentProgress` composite PK),
`apollo/persistence/done-write-linkage` (sibling Done-time helpers),
`apollo/overseer/xp` (`level_from_xp`).
