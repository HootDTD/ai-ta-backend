---
doc: apollo/projections/performance-insights
description: performance_insights — algorithmic (LLM/Neo4j-free) engagement, retry, correlation, quartile, and flag helpers behind the class-performance payload's engagement + insights blocks.
owns:
  - apollo/projections/performance_insights.py
related:
  - apollo/projections/performance
last_verified: 2026-07-30
stub: false
---

# Projections performance-insights — algorithmic engagement stats

Deterministic, **LLM/Neo4j-free** statistics that feed the v2 class-performance
payload's per-student `engagement` block, per-student `flags`, and top-level
`insights`. Everything but two thin DB loaders is a **pure function on plain
lists**, so the validity anchors exercise them with hand-computed fixtures and
no database. Composed by [performance](performance.md)'s assembler.

## Interface

- **Stat helpers (pure):** `mean`, `median`, `pearson`, `spearman` (= Pearson on
  average ranks, ties averaged, via `_average_ranks`), `word_count`.
- **Aggregations (pure):** `engagement_by_student(message_rows)` →
  `{teaching_turns, median_words}`; `problem_aggregates(attempt_rows)` →
  per-student `ProblemAgg` list (`first_score` = lowest-id graded attempt,
  `best_score` = best-wins, `best_is_last`); `retry_fields(aggs)` →
  `{problems_retried, avg_gain}`; `student_flags(...)` and `student_extras(...)`
  (the `{engagement, flags}` add-on).
- **Insight builders (pure):** `build_correlation(points)`,
  `build_effort_quartiles(students)`, `build_retry_payoff(aggregates)`,
  `build_insights(graded_points, aggregates)`.
- **DB loaders:** `load_engagement(db, *, search_space_id)` (student messages);
  `load_problem_aggregates(db, *, search_space_id, score_expr)` (graded
  attempts — `score_expr` is [performance](performance.md)'s `_SCORE_EXPR`,
  passed in so the served-grade expression lives in one place).

## Data flow

`load_engagement` reads `app.tutoring_messages` where `role = 'student'` (the
Apollo side is `role = 'apollo'`), course-scoped, joined to
`app.learning_activities` for the owning student's `user_id` (messages carry no
user_id). `load_problem_aggregates` reads graded `app.problem_attempts` ordered
by `pa.id`, plus a second pass surfacing the latest attempt id per (student,
problem) over **all** attempts (any result) that drives `best_is_last`. Both
hand raw rows to the pure folders above.

## Invariants & gotchas

- **Suppression:** `correlation` + `effort_quartiles` are null below
  `MIN_CORRELATION_N` (8) students-with-a-grade; `retry_payoff` is null when no
  (student, problem) has >= 2 graded attempts. `pearson` returns 0.0 on zero
  variance (undefined correlation reported as no signal).
- **Effort quartiles tie-break on `user_id`, NEVER grade:** equal-`teaching_turns`
  students are ordered by `user_id` (neutral, deterministic). Ordering ties by
  grade would smear equal-effort students across quartile boundaries and
  fabricate the monotonic effort->grade gradient the chart exists to test.
- **Flags are module constants:** `not_started` (0 attempts), `low_effort`
  (`>= LOW_EFFORT_MIN_TURNS` turns and `median_words < LOW_EFFORT_MAX_MEDIAN_WORDS`),
  `gave_up` (a problem best `< GAVE_UP_MAX_BEST` with no later attempt of ANY
  result — graded, ungraded, or in-progress — after the best-producing one, so a
  student mid-retry is never flagged),
  `grinding` (`>= GRINDING_MIN_ATTEMPTS` graded attempts, `best - first <=
  GRINDING_MAX_GAIN`).
- **Same served-grade semantics as v1 everywhere** — best-wins here is the same
  max-score/latest-id order `_SCORE_EXPR` produces, so retry gain never disagrees
  with the best-wins grade the student was shown.
