---
doc: apollo/conversation/handlers/browse
description: apollo/handlers/browse.py — handle_list_problems, the read-only student browse surface for one concept
owns:
  - apollo/handlers/browse.py
related:
  - apollo/conversation/routing/router
  - apollo/overseer/problem-selector
  - apollo/conversation/curriculum/db
last_verified: 2026-07-25
stub: false
---

# handlers/browse — student problem browse

`apollo/handlers/browse.py` lists a course's teachable problems for one concept
(GET `/apollo/problems`), read-only.

## Interface

- `handle_list_problems(db, *, user_id, search_space_id, concept_id, difficulty=None) -> dict`
  — called by `routing/router`. Returns `{problems: [{id, difficulty,
  problem_text, attempted}]}`.

## Data flow

The eligibility predicate is the selector's (tier-2 + non-quarantined, via
`list_problems_for_concept`, `overseer/problem-selector`). Course scope is
enforced with the same candidate set session entry uses
(`list_course_concepts`, `curriculum/db`): a `concept_id` from another course
raises `NoMatchingConceptError` (409) instead of leaking cross-course problems.
`attempted` is computed by joining the user's `ProblemAttempt` problem ids.

## Invariants & gotchas

- **Student-safety**: the payload carries ONLY `{id, difficulty, problem_text,
  attempted}` — reference solutions, private nodes, `given_values`,
  `target_unknown`, and rubric vocabulary never enter it.
- Attempted problems are flagged (not excluded) via the surrogate `database_id`.

## Related

Route wiring: `routing/router`; eligibility: `overseer/problem-selector`;
course-scope candidate set: `curriculum/db`.
