---
doc: apollo/projections/performance-insights
description: performance_insights — algorithmic (LLM/Neo4j-free) engagement, retry, correlation, quartile, and flag helpers behind the class-performance payload's engagement + insights blocks.
owns:
  - apollo/projections/performance_insights.py
related:
  - apollo/projections/performance
  - apollo/projections/classroom
  - apollo/conversation/handlers/done
last_verified: 2026-08-12
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
  `{teaching_turns, median_words}`;
  `problem_aggregates(attempt_rows, latest_attempt_ids=None, created_at_by_attempt=None)` →
  per-student `ProblemAgg` list (`first_score` = lowest-id graded attempt,
  `best_score` = best-wins, `best_is_last`, plus the display-only
  `median_gap_seconds` / `min_gap_seconds` / `first_to_best_seconds`, filled
  only when the `created_at` side map is threaded in); `retry_fields(aggs)` →
  `{problems_retried, avg_gain}`; `student_flags(...)` and `student_extras(...)`
  (the `{engagement, flags}` add-on); `gap_seconds(timestamps)` → consecutive
  ABSOLUTE deltas in seconds over one pair's id-ordered graded-attempt
  timestamps (N stamps → N−1 gaps); `teacher_visible_misconception(entry)` and
  `repeated_misconception_pairs(entry_rows)` (P3.2 — the `(user_id, problem_id)`
  set behind the 6th flag).
- **Insight builders (pure):** `build_correlation(points)`,
  `build_effort_quartiles(students)`, `build_retry_payoff(aggregates)`,
  `build_retry_timing(aggregates)`, `build_insights(graded_points, aggregates)`.
- **DB loaders:** `load_engagement(db, *, search_space_id)` (student messages);
  `load_problem_aggregates(db, *, search_space_id, score_expr, repeated_pairs=None)`
  (graded attempts — `score_expr` is [performance](performance.md)'s
  `_SCORE_EXPR`, passed in so the served-grade expression lives in one place; the
  graded SELECT also carries the display-only `pa.created_at`, threaded on as
  `created_at_by_attempt`); `load_repeated_misconception_pairs(db, *,
  search_space_id)` (canonical `internal.grading_runs` artifacts).

## Data flow

`load_engagement` reads `app.tutoring_messages` where `role = 'student'` (the
Apollo side is `role = 'apollo'`), course-scoped, joined to
`app.learning_activities` for the owning student's `user_id` (messages carry no
user_id). `load_problem_aggregates` reads graded `app.problem_attempts` ordered
by `pa.id` (one added column, `pa.created_at` — no new query, scan, or index),
plus a second pass surfacing the latest attempt id per (student, problem) over
**all** attempts (any result) that drives `best_is_last`.
`load_repeated_misconception_pairs` unrolls
`internal.grading_runs.grader_payload -> 'misconceptions'` (canonical role,
course-scoped) under the same `jsonb_typeof(...) = 'array'` guard the other two
readers of that column use, and hands whole JSONB entries to the pure fold —
the visibility and repeat decisions are Python, not SQL. All three hand raw
rows to the pure folders above.

## Invariants & gotchas

- **Suppression:** `correlation` + `effort_quartiles` are null below
  `MIN_CORRELATION_N` (8) students-with-a-grade; `retry_payoff` AND
  `retry_timing` are null when no (student, problem) has >= 2 graded attempts
  — the same gate, so both teacher strips appear and disappear together.
  `MIN_CORRELATION_N` must NOT be applied to `retry_timing`: it is a per-pair
  signal, not a population statistic. `pearson` returns 0.0 on zero variance
  (undefined correlation reported as no signal).
- **`retry_timing.median_gap_seconds` is the median of the per-pair median
  gaps** — each retried pair's own median gap is computed first, and the
  class-level value is the median across those per-pair values, NOT a single
  median taken over every raw inter-attempt gap in the class. `min_gap_seconds`
  stays exact: the minimum of each pair's minimum gap equals the true minimum
  over every raw gap.
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
  GRINDING_MAX_GAIN`), `rapid_retry` (P3.3: `graded_count >= 2` AND
  `min_gap_seconds < RAPID_RETRY_MAX_SECONDS` (300) AND `best - first >=
  RAPID_RETRY_MIN_GAIN` (30.0) — a band jump by pure arithmetic, since the
  widest `LETTER_BANDS` band F = [0, 30) is exactly 30 wide). The fast gap and
  the gain are measured over the pair as a whole, not necessarily over the same
  transition — on pairs with 3+ attempts this is a heuristic, not proof that one
  single retry did both. `repeated_misconception` (P3.2: the same
  `canonical_key` teacher-visible in `>= REPEATED_MISCONCEPTION_MIN_ATTEMPTS` (2)
  DISTINCT graded attempts by one student at one problem — distinct ATTEMPTS,
  never rows, so a key repeated inside one array is still one attempt). Stable
  order: not_started, low_effort, gave_up, grinding, rapid_retry,
  repeated_misconception. `_is_rapid_flip` is the SINGLE predicate behind both
  `rapid_retry` and `retry_timing.rapid_flips`.
- **Flags are APPEND-ONLY.** A new flag goes on the END; no existing flag's
  spelling or position may move (the teacher UI keys off these strings). Every
  P3.2 input is an optional SIDE MAP, so an absent payload reproduces the
  original five exactly — pinned by
  `tests/test_performance_repeated_misconception.py`.
- **`repeated_misconception` is a level-3 surface, gated on the WRITE side.** The
  misconception array is persisted from wrongness level 1 for internal readers
  ([done](../conversation/handlers/done.md)), so `teacher_visible_misconception`
  drops entries carrying the `shadow` marker — and `resolved` ones, which the
  student already fixed. Same two exclusions as
  [classroom](classroom.md)'s `top_misconceptions`, expressed in Python here and
  in SQL there; both are driven at levels 0/1/2/3 by
  `tests/database/test_wrongness_teacher_surfaces_postgres.py`. A pair only gets
  the flag if it also has graded attempts WITH a served score, since artifact
  rows alone never mint a `ProblemAgg`.
- **Same served-grade semantics as v1 everywhere** — best-wins here is the same
  max-score/latest-id order `_SCORE_EXPR` produces, so retry gain never disagrees
  with the best-wins grade the student was shown.
- **`created_at` is DISPLAY-ONLY (P3.3).** Timing is threaded in as an optional
  `created_at_by_attempt` side map and read in ascending ATTEMPT-ID order;
  `gap_seconds` takes absolute deltas so a clock-skewed row can't report a
  negative duration. No ordering, best-wins selection, or score expression
  anywhere is keyed on a timestamp — re-ordering by time would silently change
  served grades and break teacher/student grade parity.
