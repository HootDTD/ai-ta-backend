---
doc: apollo/projections/performance
description: class_performance — the teacher classroom-performance payload (roster, best-wins grades, distribution, activity, rubric-loss signal, concept rollup) aggregated from served grade snapshots.
owns:
  - apollo/projections/performance.py
related:
  - apollo/projections/classroom
  - apollo/overseer/topic-score
  - apollo/conversation/routing/router
last_verified: 2026-07-30
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
  concepts, students}` in one payload; every list is roster-bounded (a course
  is tens of students), not a cross-course export.

## Data flow

All reads are raw SQL over durable rows: `app.problem_attempts` (grades +
activity), `app.course_memberships` (roster), `app.student_progress`
(XP/level + "signed in but never attempted"), `app.problems` →
`app.concepts` (concept rollup), and Supabase-managed `auth.users`
(identity). The grade of one attempt is
`diagnostic_report -> 'served_overall'` (the served [topic
score](../overseer/topic-score.md) snapshot) with
`diagnostic_report -> 'rubric' -> 'overall'` as the pre-snapshot fallback;
"best-attempt-wins" per (student, problem) — `DISTINCT ON ... ORDER BY score
DESC` — matches the student-facing browse semantics, and the winning
attempt's letter is carried verbatim (`score_to_letter` is only a fallback
for a missing letter and for banding the *averages*, using the grader's own
`LETTER_BANDS`).

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
- **`auth.users` lookup is failure-isolated**: outside `Base.metadata`
  (absent from the Testcontainers schema), queried under a `begin_nested()`
  SAVEPOINT so a missing table / revoked grant degrades to null identities
  without voiding the payload or poisoning the transaction.
- `activity_by_day` buckets by the attempt's UTC date; "in progress" is any
  non-`graded` result (including NULL).
