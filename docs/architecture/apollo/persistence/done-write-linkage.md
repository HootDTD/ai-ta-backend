---
doc: apollo/persistence/done-write-linkage
description: Two small pure read-side resolvers the Done-time XP/mastery writers call — prior-graded-attempt detection and durable problem-bank id resolution.
owns:
  - apollo/persistence/attempt_history.py
  - apollo/persistence/problem_linkage.py
related:
  - apollo/persistence/_index
  - apollo/persistence/models
  - apollo/persistence/progress-repo
  - apollo/conversation/handlers/done
  - apollo/projections/mastery
last_verified: 2026-07-25
stub: false
---

# apollo/persistence/done-write-linkage

Two small pure read-side resolvers (scoped SELECTs over the models, no commit)
that the Done-time XP/mastery writers call.

## Interface

- **`has_prior_graded_attempt(*, db, user_id, course_id, problem_id,
  exclude_attempt_id) → bool`** — counts any OTHER `ProblemAttempt` for the same
  `(user, course, problem)` whose `result` is in `GRADED_ATTEMPT_RESULTS`. This
  is the cross-session re-attempt signal the XP awarder uses.
- **`resolve_problem_id(db, *, concept_id, course_id, problem_identity) →
  int | None`** — resolves a public `problem_code` (str) OR an internal bigint,
  within one course/concept, to the durable `app.problems.id` for mastery-event
  linkage.

## Data flow

Both are consumed by `apollo/conversation/handlers/done` and
`apollo/projections/mastery` on the Done write path — `has_prior_graded_attempt`
gates the re-attempt XP branch; `resolve_problem_id` supplies the
`MasteryEvent.concept_problem_id` linkage.

## Invariants & gotchas

- **`abandoned` is excluded from graded** (it is a mid-problem switch, not a
  grade); within-session `/retry` overwrites the same row, so it is handled
  upstream by checking the attempt's own `result` before this call — hence the
  `exclude_attempt_id` self-exclusion.
- **Tier-2 teachable rows win over Tier-1 inventory twins** (`ORDER BY tier DESC,
  id DESC`); newest id is the deterministic tiebreak; quarantined rows are
  excluded.
- A legacy/fixture code with no bank row resolves to `None` — the mastery event
  stays valid with a **NULL linkage**.

## Related

`apollo/persistence/models` (`ATTEMPT_RESULTS`/`GRADED_ATTEMPT_RESULTS`,
`Problem.tier`/`quarantined_at`/`problem_code`),
`apollo/persistence/progress-repo` (sibling Done-time helper),
`apollo/projections/mastery` (co-caller).
