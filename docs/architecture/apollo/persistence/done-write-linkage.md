---
doc: apollo/persistence/done-write-linkage
description: Small read-side resolvers the Done-time XP/mastery/questioning writers call — prior-graded-attempt detection, durable problem-bank id resolution, and the cross-attempt wrongness read.
owns:
  - apollo/persistence/attempt_history.py
  - apollo/persistence/problem_linkage.py
related:
  - apollo/persistence/_index
  - apollo/persistence/models
  - apollo/persistence/progress-repo
  - apollo/conversation/handlers/done
  - apollo/projections/mastery
  - apollo/overseer/wrongness
  - apollo/projections/classroom
last_verified: 2026-08-12
stub: false
---

# apollo/persistence/done-write-linkage

Small read-side resolvers (scoped SELECTs, no commit) that the Done-time
XP/mastery writers and the questioning loop call.

## Interface

- **`has_prior_graded_attempt(*, db, user_id, course_id, problem_id,
  exclude_attempt_id) → bool`** — counts any OTHER `ProblemAttempt` for the same
  `(user, course, problem)` whose `result` is in `GRADED_ATTEMPT_RESULTS`. This
  is the cross-session re-attempt signal the XP awarder uses.
- **`resolve_problem_id(db, *, concept_id, course_id, problem_identity) →
  int | None`** — resolves a public `problem_code` (str) OR an internal bigint,
  within one course/concept, to the durable `app.problems.id` for mastery-event
  linkage.
- **`prior_wrongness_findings(db, *, attempt_id, problem_id, course_id,
  limit=8) → tuple[dict, ...]`** (Apollo P3.2 L2c/D4) — corroborated findings
  this SAME user recorded on this SAME problem in EARLIER attempts, newest-first,
  as `{canonical_key, evidence_span, resolved, attempt_id}`. ONE raw-SQL query:
  `internal.grading_runs` (`role='canonical'`) JOINed to `app.problem_attempts`
  to resolve the caller's `user_id` from `attempt_id`, with a `LATERAL
  jsonb_array_elements` over `grader_payload -> 'misconceptions'` — the same
  shape `projections/classroom.top_misconceptions` uses.

## Data flow

The first two are consumed by `apollo/conversation/handlers/done` and
`apollo/projections/mastery` on the Done write path — `has_prior_graded_attempt`
gates the re-attempt XP branch; `resolve_problem_id` supplies the
`MasteryEvent.concept_problem_id` linkage. `prior_wrongness_findings` is read at
attempt time by `apollo/conversation/questioning/controller` (to carry at most
ONE prior challenge forward as a question) and at Done by `handlers/done` (to
dedup the decision-7 XP bonus once per user × problem × node).

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
- **`prior_wrongness_findings` carries the QUESTION, never the punishment.** The
  consequence is always earned inside the current attempt; the caller phrases it
  as continuity and never mentions grades, attempts, retries, or penalties. It
  reads the append-only artifact — **no new table, and specifically never the
  retired `apollo_misconception_observations`** (frozen legacy chain).
- **It has its OWN failure domain:** any exception logs
  `apollo_prior_findings_failed` and returns `()`. A lost memory costs one
  continuity question; a raise would reach the Done grade path.
- **`grader_payload` is free-form JSONB**, so the LATERAL guards on
  `jsonb_typeof(... ) = 'array'` — an object or absent key yields zero rows
  instead of raising "cannot extract elements from an object" mid-Done.
- `problem_id` must be the durable `app.problems.id` (what `GradingRun.problem_id`
  stores), not a public `problem_code` — pair it with `resolve_problem_id`.
- **Index note:** `internal.grading_runs` holds ~159 rows, so the prior-findings
  read is a seq scan by design. Past a few thousand rows add `(user_id,
  problem_id)` — the WHERE clause is exactly that prefix.
- **The `has_prior_graded_attempt` TOCTOU is closed upstream** (M1/P3.4): it is
  an unlocked `SELECT COUNT`, so two concurrent Dones both used to read "no
  prior graded attempt" and both award first-attempt XP. Done's CAS claim now
  serializes them, so only one caller can be inside this read at a time.

## Related

`apollo/persistence/models` (`ATTEMPT_RESULTS`/`GRADED_ATTEMPT_RESULTS`,
`Problem.tier`/`quarantined_at`/`problem_code`),
`apollo/persistence/progress-repo` (sibling Done-time helper),
`apollo/projections/mastery` (co-caller).
