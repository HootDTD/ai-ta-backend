---
doc: apollo/conversation/handlers/browse
description: apollo/handlers/browse.py — handle_list_problems, the read-only student browse surface for one concept
owns:
  - apollo/handlers/browse.py
related:
  - apollo/conversation/routing/router
  - apollo/overseer/problem-selector
  - apollo/conversation/curriculum/db
last_verified: 2026-07-27
stub: false
---

# handlers/browse — student problem browse

`apollo/handlers/browse.py` lists a course's teachable problems for one concept
(GET `/apollo/problems`), read-only.

## Interface

- `handle_list_problems(db, *, user_id, search_space_id, concept_id, difficulty=None) -> dict`
  — called by `routing/router`. Returns `{problems: [{id, difficulty,
  problem_text, attempted, grade}]}` where `grade` is `{score, letter,
  feedback}` (the student's own best served overall across graded attempts,
  plus that same attempt's narrative) or `null`.
- `served_overall_from_report(report) -> {score, letter} | None` — extracts the
  student-facing overall from a persisted `diagnostic_report`: prefers the
  Done path's `served_overall` snapshot, falls back to the legacy
  `rubric.overall`; returns `None` on any malformed shape.
- `feedback_from_report(report) -> str | None` — extracts the report's
  `narrative` (the exact feedback text the Done panel served; for topic-score
  attempts, the flattened structured feedback). Anything but a non-empty
  string → `None`.

## Data flow

The eligibility predicate is the selector's (tier-2 + non-quarantined, via
`list_problems_for_concept`, `overseer/problem-selector`). Course scope is
enforced with the same candidate set session entry uses
(`list_course_concepts`, `curriculum/db`): a `concept_id` from another course
raises `NoMatchingConceptError` (409) instead of leaking cross-course problems.
One query loads the user's `ProblemAttempt` rows (problem_id, result,
diagnostic_report) for the course: `attempted` is any row on the problem;
`grade` is the best (highest score, ties → latest) `served_overall_from_report`
over rows with `result == "graded"` — the narrow literal on purpose (same
contract as the progress dashboard: only the Done path writes both
`result="graded"` and a rubric-shaped report). `grade.feedback` (2026-07-27)
is `feedback_from_report` of the SAME winning row — feedback never comes from
a different attempt than the displayed grade.

## Invariants & gotchas

- **Student-safety**: the payload carries ONLY `{id, difficulty, problem_text,
  attempted, grade}` — reference solutions, private nodes, `given_values`,
  `target_unknown`, and rubric vocabulary never enter it. `grade` is always
  the requesting student's own; other students' attempts never surface.
  `grade.feedback` is only the narrative that attempt already served to this
  student at Done-time — rubric/coverage internals stay out.
- Attempted problems are flagged (not excluded) via the surrogate `database_id`.
- A malformed/legacy report degrades that problem to the plain attempted state
  (`grade: null`) — it must never 500 the browse surface. A graded report with
  an unusable narrative degrades `feedback` alone to `null`, never the chip.

## Related

Route wiring: `routing/router`; eligibility: `overseer/problem-selector`;
course-scope candidate set: `curriculum/db`.
