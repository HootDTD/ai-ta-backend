---
doc: apollo/projections/performance
description: class_performance — the teacher classroom-performance payload (roster, best-wins grades, distribution, activity, rubric-loss signal, concept + per-problem rollups, per-student engagement + flags) aggregated from served grade snapshots.
owns:
  - apollo/projections/performance.py
related:
  - apollo/projections/performance-insights
  - apollo/projections/performance-problems
  - apollo/projections/classroom
  - apollo/overseer/topic-score
  - apollo/conversation/routing/router
last_verified: 2026-07-31
stub: false
---

# Projections performance — teacher class-performance payload

One pure read-side aggregation, `class_performance(db, *, search_space_id) ->
dict`, backing `GET /apollo/teacher/classroom/{search_space_id}/performance`
(registered in `apollo/api.py`, teacher-role gated like the other classroom
endpoints). No new grading, inference, LLM, or Neo4j.

## Interface

- `class_performance(db, *, search_space_id)` — returns `{roster, totals,
  class_average, grade_distribution, activity_by_day, rubric_averages,
  concepts, problems, students, insights}` in one payload (v2); every list is
  roster-bounded (a course is tens of students), not a cross-course export.
- **v2 payload deltas** (design spec 2026-07-30): `rubric_averages` dropped its
  fourth `misconception_corrected` axis; each `students[]` row dropped `xp`/
  `level` and gained an `engagement` block + `flags` list; NEW top-level
  `problems` (per-problem best-wins letter distribution, grouped by concept) and
  `insights` (correlation / effort quartiles / retry payoff). The `engagement`,
  `flags`, and `insights` composition lives in
  [performance-insights](performance-insights.md).
- **v2.1 payload deltas** (design spec 2026-07-31 addendum, additive only): each
  `problems[]` row gained `problem_text` (full statement), `students`
  (per-problem best-wins `{user_id, email, score, letter}`, score desc) and
  `nodes` (per graded reference node: understood/partial/missed/graded counts
  over each student's BEST graded attempt). The whole `problems` block — plus the
  shared `letter_distribution` behind `grade_distribution` — now lives in
  [performance-problems](performance-problems.md); this leaf's assembler threads
  the best-wins rows + identities into it.

## Data flow

All reads are raw SQL over durable rows: `app.problem_attempts` (grades +
activity + retry aggregates), `app.course_memberships` (roster),
`app.student_progress` (the "signed in but never attempted" set + those
students' `last_active` — xp/level are no longer surfaced), `app.problems` →
`app.concepts` (concept + per-problem rollups), `app.tutoring_messages` →
`app.learning_activities` (student teaching turns, via
[performance-insights](performance-insights.md)), and Supabase-managed
`auth.users` (identity). The grade of one attempt is
`diagnostic_report -> 'served_overall'` (the served [topic
score](../overseer/topic-score.md) snapshot) with
`diagnostic_report -> 'rubric' -> 'overall'` as the pre-snapshot fallback;
"best-attempt-wins" per (student, problem) — `DISTINCT ON ... ORDER BY score
DESC` — matches the student-facing browse semantics, and the winning
attempt's letter is carried verbatim (`score_to_letter` is only a fallback
for a missing letter and for banding the *averages*, using the grader's own
`LETTER_BANDS`). The single `_SCORE_EXPR` served-grade fragment is passed into
`performance_insights.load_problem_aggregates` so retry/first-vs-best stays
byte-identical to best-wins.

## Invariants & gotchas

- **Reads what the LIVE path writes.** Unlike `classroom.mastery_heatmap`
  (over `app.learner_state`, empty until `APOLLO_GRAPH_SIM_LAYER3_ENABLED`
  flips), every source here is populated by the flag-independent grading /
  progress paths — the panel fills itself as students work.
- **Teacher and student always see the same grade** — the served letter is
  never re-derived from the score.
- **`rubric_averages` deliberately averages ALL graded attempts** (retries
  included): it is the "where does the class lose points" signal, not the
  served grade.
- **`problems` is best-wins per (student, problem)**, built by
  [performance-problems](performance-problems.md) from the same `best_rows` (and
  the shared `letter_distribution`) as `grade_distribution`, so the two never
  disagree; its `nodes` drill-down reuses the served topic score's own
  `_credit_for_node`, so it can't disagree with the grade either. `_best_graded_rows`
  carries each best attempt's `diagnostic_report -> 'coverage'` (coverage only) to
  feed it.
- **`auth.users` lookup is failure-isolated**: outside `Base.metadata`
  (absent from the Testcontainers schema), queried under a `begin_nested()`
  SAVEPOINT so a missing table / revoked grant degrades to null identities
  without voiding the payload or poisoning the transaction. In prod the read
  needs the column-level grant from migration `049_auth_users_identity_grant`
  ([database/supabase-migrations](../../database/supabase-migrations.md)); before
  it lands the panel shows shortened user ids, not emails.
- `activity_by_day` buckets by the attempt's UTC date; "in progress" is any
  non-`graded` result (including NULL).
